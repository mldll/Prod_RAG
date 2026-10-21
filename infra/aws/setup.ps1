# ==============================================================================
# Dynamic & Portable AWS Provisioning & GitHub OIDC Setup Script (AWS Lambda Container)
# ==============================================================================
param(
    [string]$Region = "us-east-1",
    [string]$AppName = "ragprod",
    [string]$GitHubRepo = "mldll/Prod_RAG"
)

# Helper function to execute AWS CLI commands safely and verify exit code
function Invoke-AwsCmd {
    param(
        [string[]]$CmdArgs
    )
    # Windows PowerShell turns redirected native stderr into errors, which a caller's "Stop" preference makes terminating
    $ErrorActionPreference = "Continue"
    $output = & aws @CmdArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        $errorMsg = ($output | Out-String).Trim()
        throw "AWS CLI command failed (aws $($CmdArgs -join ' ')):`n$errorMsg"
    }
    return $output
}

# Helper function to test if an AWS resource exists without throwing native errors
function Test-AwsResourceExists {
    param(
        [string[]]$CmdArgs
    )
    # As in Invoke-AwsCmd: a "not found" on stderr must return $false, not throw
    $ErrorActionPreference = "Continue"
    $null = & aws @CmdArgs 2>$null
    return ($LASTEXITCODE -eq 0)
}

# Helper function to write UTF-8 files WITHOUT Byte Order Mark (BOM)
function Write-JsonFileNoBOM {
    param(
        [string]$FilePath,
        [string]$JsonContent
    )
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($FilePath, $JsonContent, $utf8NoBom)
}

Write-Host "1. Checking AWS CLI authentication..." -ForegroundColor Cyan
$callerIdentityJson = Invoke-AwsCmd -CmdArgs @("sts", "get-caller-identity", "--output", "json")
$callerIdentity = $callerIdentityJson | ConvertFrom-Json
$awsAccountId = $callerIdentity.Account
Write-Host "   Authenticated to AWS Account ID: $awsAccountId (Region: $Region)" -ForegroundColor Green

# 1. AWS ECR Repository
$ecrRepoName = "rag-api"
Write-Host "`n2. Ensuring ECR Repository '$ecrRepoName' exists..." -ForegroundColor Cyan
if (Test-AwsResourceExists -CmdArgs @("ecr", "describe-repositories", "--repository-names", $ecrRepoName, "--region", $Region)) {
    Write-Host "   ECR Repository '$ecrRepoName' already exists." -ForegroundColor Green
} else {
    Invoke-AwsCmd -CmdArgs @("ecr", "create-repository", "--repository-name", $ecrRepoName, "--region", $Region) | Out-Null
    Write-Host "   Created ECR Repository '$ecrRepoName'." -ForegroundColor Green
}
$ecrUri = "$awsAccountId.dkr.ecr.$Region.amazonaws.com/$ecrRepoName"

# Ensure ECR Repository Policy grants Lambda service pull access
$ecrPolicyJson = @{
    Version = "2012-10-17"
    Statement = @(
        @{
            Sid = "LambdaECRImageRetrievalPolicy"
            Effect = "Allow"
            Principal = @{ Service = "lambda.amazonaws.com" }
            Action = @("ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer")
        }
    )
} | ConvertTo-Json -Depth 5

$tempEcrPol = [System.IO.Path]::GetTempFileName()
try {
    Write-JsonFileNoBOM -FilePath $tempEcrPol -JsonContent $ecrPolicyJson
    Invoke-AwsCmd -CmdArgs @("ecr", "set-repository-policy", "--repository-name", $ecrRepoName, "--policy-text", "file://$tempEcrPol", "--region", $Region) | Out-Null
} finally {
    Remove-Item $tempEcrPol -ErrorAction SilentlyContinue
}

# 2. AWS Secrets Manager Seeding
Write-Host "`n3. Seeding secrets into AWS Secrets Manager..." -ForegroundColor Cyan
$secretNames = @("GROQ_API_KEY", "GOOGLE_API_KEY")
foreach ($secretName in $secretNames) {
    # Typed without echo, and handed to the CLI through a BOM-less temp file so the key never appears on a command line
    $secureKey = Read-Host "Enter $secretName (press Enter to skip if already created)" -AsSecureString
    if ($secureKey.Length -eq 0) {
        continue
    }
    $tempKey = [System.IO.Path]::GetTempFileName()
    try {
        Write-JsonFileNoBOM -FilePath $tempKey -JsonContent ([System.Net.NetworkCredential]::new("", $secureKey).Password)
        if (Test-AwsResourceExists -CmdArgs @("secretsmanager", "describe-secret", "--secret-id", $secretName, "--region", $Region)) {
            Invoke-AwsCmd -CmdArgs @("secretsmanager", "put-secret-value", "--secret-id", $secretName, "--secret-string", "file://$tempKey", "--region", $Region) | Out-Null
            Write-Host "   Updated $secretName in AWS Secrets Manager." -ForegroundColor Green
        } else {
            Invoke-AwsCmd -CmdArgs @("secretsmanager", "create-secret", "--name", $secretName, "--secret-string", "file://$tempKey", "--region", $Region) | Out-Null
            Write-Host "   Created $secretName in AWS Secrets Manager." -ForegroundColor Green
        }
    } finally {
        Remove-Item $tempKey -ErrorAction SilentlyContinue
    }
}

# 3. IAM Execution Role for AWS Lambda Container
Write-Host "`n4. Setting up IAM Execution Role for AWS Lambda..." -ForegroundColor Cyan
$lambdaRoleName = "LambdaExecutionRole-$AppName"
$lambdaTrustPolicy = @{
    Version = "2012-10-17"
    Statement = @(
        @{
            Effect = "Allow"
            Principal = @{ Service = "lambda.amazonaws.com" }
            Action = "sts:AssumeRole"
        }
    )
} | ConvertTo-Json -Depth 3

# Only this app's secrets; Secrets Manager appends "-" and 6 random characters to each secret's ARN
$secretsPolicy = @{
    Version = "2012-10-17"
    Statement = @(
        @{
            Effect = "Allow"
            Action = @("secretsmanager:GetSecretValue")
            Resource = @($secretNames | ForEach-Object { "arn:aws:secretsmanager:${Region}:${awsAccountId}:secret:$($_)-??????" })
        }
    )
} | ConvertTo-Json -Depth 4

# The role is created once; its trust and inline policies are rewritten on every run, so a rerun updates an existing role
$tempTrust = [System.IO.Path]::GetTempFileName()
$tempPolicy = [System.IO.Path]::GetTempFileName()
try {
    Write-JsonFileNoBOM -FilePath $tempTrust -JsonContent $lambdaTrustPolicy
    if (Test-AwsResourceExists -CmdArgs @("iam", "get-role", "--role-name", $lambdaRoleName)) {
        Invoke-AwsCmd -CmdArgs @("iam", "update-assume-role-policy", "--role-name", $lambdaRoleName, "--policy-document", "file://$tempTrust") | Out-Null
        Write-Host "   IAM Role '$lambdaRoleName' already exists; trust policy rewritten." -ForegroundColor Green
    } else {
        Invoke-AwsCmd -CmdArgs @("iam", "create-role", "--role-name", $lambdaRoleName, "--assume-role-policy-document", "file://$tempTrust") | Out-Null
        Invoke-AwsCmd -CmdArgs @("iam", "attach-role-policy", "--role-name", $lambdaRoleName, "--policy-arn", "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole") | Out-Null
        Write-Host "   Created IAM Role '$lambdaRoleName'." -ForegroundColor Green
    }

    # Lambda pulls the image with its own service principal through the repository policy above, so the role needs no ECR access
    $ecrReadOnlyArn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
    $attached = Invoke-AwsCmd -CmdArgs @("iam", "list-attached-role-policies", "--role-name", $lambdaRoleName, "--query", "AttachedPolicies[].PolicyArn", "--output", "text")
    if ((($attached | Out-String) -split "\s+") -contains $ecrReadOnlyArn) {
        Invoke-AwsCmd -CmdArgs @("iam", "detach-role-policy", "--role-name", $lambdaRoleName, "--policy-arn", $ecrReadOnlyArn) | Out-Null
        Write-Host "   Detached AmazonEC2ContainerRegistryReadOnly from '$lambdaRoleName'." -ForegroundColor Green
    }

    Write-JsonFileNoBOM -FilePath $tempPolicy -JsonContent $secretsPolicy
    Invoke-AwsCmd -CmdArgs @("iam", "put-role-policy", "--role-name", $lambdaRoleName, "--policy-name", "SecretsManagerRead", "--policy-document", "file://$tempPolicy") | Out-Null
    Write-Host "   Read access to $($secretNames -join ', ') applied to '$lambdaRoleName'." -ForegroundColor Green
} finally {
    Remove-Item $tempTrust -ErrorAction SilentlyContinue
    Remove-Item $tempPolicy -ErrorAction SilentlyContinue
}
$lambdaRoleArn = "arn:aws:iam::${awsAccountId}:role/$lambdaRoleName"

# 4b. S3 bucket for the state snapshot, and Lambda access to it
$stateBucket = "$AppName-state-$awsAccountId"
Write-Host "`n4b. Ensuring S3 state bucket '$stateBucket' exists..." -ForegroundColor Cyan
if (Test-AwsResourceExists -CmdArgs @("s3api", "head-bucket", "--bucket", $stateBucket)) {
    Write-Host "   S3 bucket '$stateBucket' already exists." -ForegroundColor Green
} else {
    $createArgs = @("s3api", "create-bucket", "--bucket", $stateBucket, "--region", $Region)
    if ($Region -ne "us-east-1") {
        $createArgs += @("--create-bucket-configuration", "LocationConstraint=$Region")
    }
    Invoke-AwsCmd -CmdArgs $createArgs | Out-Null
    Write-Host "   Created S3 bucket '$stateBucket'." -ForegroundColor Green
}

# Versioning keeps previous snapshots for rollback; public access stays blocked
Invoke-AwsCmd -CmdArgs @("s3api", "put-bucket-versioning", "--bucket", $stateBucket, "--versioning-configuration", "Status=Enabled") | Out-Null
Invoke-AwsCmd -CmdArgs @("s3api", "put-public-access-block", "--bucket", $stateBucket, "--public-access-block-configuration", "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true") | Out-Null

$lifecycleJson = @{
    Rules = @(
        @{
            ID = "expire-old-snapshot-versions"
            Status = "Enabled"
            Filter = @{ Prefix = "" }
            NoncurrentVersionExpiration = @{ NoncurrentDays = 30 }
        }
    )
} | ConvertTo-Json -Depth 5

$stateAccessPolicy = @{
    Version = "2012-10-17"
    Statement = @(
        @{
            Effect = "Allow"
            Action = @("s3:GetObject", "s3:PutObject")
            Resource = "arn:aws:s3:::$stateBucket/*"
        },
        @{
            # Without ListBucket, S3 answers 403 instead of 404 for a missing snapshot
            Effect = "Allow"
            Action = "s3:ListBucket"
            Resource = "arn:aws:s3:::$stateBucket"
        }
    )
} | ConvertTo-Json -Depth 5

$tempLifecycle = [System.IO.Path]::GetTempFileName()
$tempStatePolicy = [System.IO.Path]::GetTempFileName()
try {
    Write-JsonFileNoBOM -FilePath $tempLifecycle -JsonContent $lifecycleJson
    Invoke-AwsCmd -CmdArgs @("s3api", "put-bucket-lifecycle-configuration", "--bucket", $stateBucket, "--lifecycle-configuration", "file://$tempLifecycle") | Out-Null

    # Applied on every run: the role-creation branch above is skipped when the role already exists
    Write-JsonFileNoBOM -FilePath $tempStatePolicy -JsonContent $stateAccessPolicy
    Invoke-AwsCmd -CmdArgs @("iam", "put-role-policy", "--role-name", $lambdaRoleName, "--policy-name", "StateSnapshotAccess", "--policy-document", "file://$tempStatePolicy") | Out-Null
    Write-Host "   Versioning, lifecycle and Lambda access configured for '$stateBucket'." -ForegroundColor Green
} finally {
    Remove-Item $tempLifecycle -ErrorAction SilentlyContinue
    Remove-Item $tempStatePolicy -ErrorAction SilentlyContinue
}

# 4. GitHub Actions OIDC Federated Role
Write-Host "`n5. Setting up GitHub Actions OIDC Federated Role..." -ForegroundColor Cyan
$oidcProviderArn = "arn:aws:iam::${awsAccountId}:oidc-provider/token.actions.githubusercontent.com"

if (Test-AwsResourceExists -CmdArgs @("iam", "get-open-id-connect-provider", "--open-id-connect-provider-arn", $oidcProviderArn)) {
    Write-Host "   GitHub OIDC Provider already exists." -ForegroundColor Green
} else {
    Invoke-AwsCmd -CmdArgs @(
        "iam", "create-open-id-connect-provider",
        "--url", "https://token.actions.githubusercontent.com",
        "--client-id-list", "sts.amazonaws.com",
        "--thumbprint-list", "6938fd5d98bab03faadb97b34396831e3780aea1", "1c58a21d81938b15d7f8d59bb29c1583fd2ec1cf", "74966d2e0878180753200c682b13280058e57f20"
    ) | Out-Null
    Write-Host "   Created GitHub OIDC Provider in IAM." -ForegroundColor Green
}

$githubRoleName = "github-actions-ragprod-aws-role"

# Trust only the main branch of this repository, in both subject formats GitHub issues:
# "repo:OWNER/REPO:..." and, for newer or opted-in repositories, "repo:OWNER@OWNER_ID/REPO@REPO_ID:..."
try {
    $githubApi = Invoke-RestMethod -Uri "https://api.github.com/repos/$GitHubRepo" -UserAgent "PowerShell" -ErrorAction Stop
    if (-not ($githubApi.full_name -and $githubApi.id -and $githubApi.owner.id)) {
        throw "unexpected response from the GitHub API"
    }
    # full_name has the exact spelling: IAM compares the subject case-sensitively
    $ghOwner, $ghRepo = $githubApi.full_name.Split('/')
    $taggedSubject = "repo:${ghOwner}@$($githubApi.owner.id)/${ghRepo}@$($githubApi.id):ref:refs/heads/main"
} catch {
    $ghOwner, $ghRepo = $GitHubRepo.Split('/')
    # "@" can't appear in GitHub owner or repository names, so these wildcards can only stand for the numeric IDs
    $taggedSubject = "repo:${ghOwner}@*/${ghRepo}@*:ref:refs/heads/main"
    Write-Warning "GitHub API lookup of '$GitHubRepo' failed ($($_.Exception.Message)). The role trusts '$taggedSubject' until you rerun this script with the lookup working."
}
$plainSubject = "repo:${ghOwner}/${ghRepo}:ref:refs/heads/main"
Write-Host "   Trusted OIDC subjects: $plainSubject" -ForegroundColor Green
Write-Host "                          $taggedSubject" -ForegroundColor Green

$githubTrustPolicy = @{
    Version = "2012-10-17"
    Statement = @(
        @{
            Effect = "Allow"
            Principal = @{ Federated = $oidcProviderArn }
            Action = "sts:AssumeRoleWithWebIdentity"
            Condition = @{
                StringEquals = @{ "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
                StringLike = @{ "token.actions.githubusercontent.com:sub" = @($plainSubject, $taggedSubject) }
            }
        }
    )
} | ConvertTo-Json -Depth 6

$lambdaFunctionName = "ragprod-api"  # created and updated by deploy.yml, restarted by collect-data.yml
$ecrRepoArn = "arn:aws:ecr:${Region}:${awsAccountId}:repository/$ecrRepoName"
$lambdaFunctionArn = "arn:aws:lambda:${Region}:${awsAccountId}:function:$lambdaFunctionName"

$deployPolicy = @{
    Version = "2012-10-17"
    Statement = @(
        @{
            # AWS doesn't support resource-level permissions for this action
            Effect = "Allow"
            Action = "ecr:GetAuthorizationToken"
            Resource = "*"
        },
        @{
            Effect = "Allow"
            Action = @(
                "ecr:BatchCheckLayerAvailability",
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchGetImage",
                "ecr:PutImage",
                "ecr:InitiateLayerUpload",
                "ecr:UploadLayerPart",
                "ecr:CompleteLayerUpload"
            )
            Resource = $ecrRepoArn
        },
        @{
            # What deploy.yml and collect-data.yml call ("aws lambda wait function-updated" polls GetFunctionConfiguration)
            Effect = "Allow"
            Action = @(
                "lambda:CreateFunction",
                "lambda:UpdateFunctionCode",
                "lambda:UpdateFunctionConfiguration",
                "lambda:GetFunction",
                "lambda:GetFunctionConfiguration",
                "lambda:CreateFunctionUrlConfig",
                "lambda:GetFunctionUrlConfig",
                "lambda:AddPermission"
            )
            Resource = $lambdaFunctionArn
        },
        @{
            Effect = "Allow"
            Action = "iam:PassRole"
            Resource = $lambdaRoleArn
            Condition = @{ StringEquals = @{ "iam:PassedToService" = "lambda.amazonaws.com" } }
        }
    )
} | ConvertTo-Json -Depth 5

# The role is created once; its trust and inline policies are rewritten on every run, so a rerun updates an existing role
$tempTrust = [System.IO.Path]::GetTempFileName()
$tempPolicy = [System.IO.Path]::GetTempFileName()
try {
    Write-JsonFileNoBOM -FilePath $tempTrust -JsonContent $githubTrustPolicy
    if (Test-AwsResourceExists -CmdArgs @("iam", "get-role", "--role-name", $githubRoleName)) {
        Invoke-AwsCmd -CmdArgs @("iam", "update-assume-role-policy", "--role-name", $githubRoleName, "--policy-document", "file://$tempTrust") | Out-Null
        Write-Host "   GitHub Actions IAM Role '$githubRoleName' already exists; trust policy rewritten." -ForegroundColor Green
    } else {
        Invoke-AwsCmd -CmdArgs @("iam", "create-role", "--role-name", $githubRoleName, "--assume-role-policy-document", "file://$tempTrust") | Out-Null
        Write-Host "   Created GitHub Actions OIDC IAM Role '$githubRoleName'." -ForegroundColor Green
    }

    Write-JsonFileNoBOM -FilePath $tempPolicy -JsonContent $deployPolicy
    Invoke-AwsCmd -CmdArgs @("iam", "put-role-policy", "--role-name", $githubRoleName, "--policy-name", "GitHubActionsDeployPolicy", "--policy-document", "file://$tempPolicy") | Out-Null
    Write-Host "   Deploy policy applied to '$githubRoleName'." -ForegroundColor Green
} finally {
    Remove-Item $tempTrust -ErrorAction SilentlyContinue
    Remove-Item $tempPolicy -ErrorAction SilentlyContinue
}
$githubRoleArn = "arn:aws:iam::${awsAccountId}:role/$githubRoleName"

# Applied on every run, like the Lambda's: the scheduled collect-data workflow reads and publishes state snapshots
$tempGithubStatePolicy = [System.IO.Path]::GetTempFileName()
try {
    Write-JsonFileNoBOM -FilePath $tempGithubStatePolicy -JsonContent $stateAccessPolicy
    Invoke-AwsCmd -CmdArgs @("iam", "put-role-policy", "--role-name", $githubRoleName, "--policy-name", "StateSnapshotAccess", "--policy-document", "file://$tempGithubStatePolicy") | Out-Null
    Write-Host "   State snapshot access applied to '$githubRoleName'." -ForegroundColor Green
} finally {
    Remove-Item $tempGithubStatePolicy -ErrorAction SilentlyContinue
}

Write-Host "`n=== AWS INFRASTRUCTURE & OIDC SETUP COMPLETE ===" -ForegroundColor Green
Write-Host "Add the following SECRETS to your GitHub Repository (Settings -> Secrets and variables -> Actions):" -ForegroundColor Yellow
Write-Host "AWS_ROLE_TO_ASSUME:    $githubRoleArn"
Write-Host "`nAdd the following VARIABLES (Settings -> Secrets and variables -> Actions -> Variables):" -ForegroundColor Yellow
Write-Host "AWS_REGION:            $Region"
Write-Host "AWS_ECR_REPO:          $ecrUri"
Write-Host "AWS_LAMBDA_ROLE_ARN:   $lambdaRoleArn"
Write-Host "AWS_STATE_BUCKET:      $stateBucket"
