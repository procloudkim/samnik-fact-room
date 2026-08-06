[CmdletBinding()]
param(
    [ValidateSet("Candidate", "Tracked", "Staged")]
    [string]$Mode = "Candidate"
)

$ErrorActionPreference = "Stop"

function Get-GitPaths {
    param([string]$Selection)

    $arguments = @(switch ($Selection) {
        "Candidate" { @("ls-files", "--cached", "--others", "--exclude-standard") }
        "Tracked" { @("ls-files") }
        "Staged" { @("diff", "--cached", "--name-only", "--diff-filter=ACMR") }
    })

    $output = & git -c core.quotepath=false @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git path enumeration failed"
    }
    return @($output | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) })
}

$allowedDocs = @(
    "docs/README.md",
    "docs/ARCHITECTURE.ko.md",
    "docs/PROJECT_STORY.md",
    "docs/AI_COLLABORATION.md",
    "docs/INTERVIEW_GUIDE.md",
    "docs/samnik-fact-room-workflow-wireframe.svg"
)
$forbiddenExtensions = @(
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".zip", ".7z", ".rar", ".mp4", ".mov", ".webm"
)
$binaryExtensions = @(
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2",
    ".ttf", ".eot", ".pyc"
)
$contentRules = [ordered]@{
    "private-key" = "-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    "github-token" = "(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}"
    "provider-api-key" = "(?:sk-proj-|sk-live-|xai-)[A-Za-z0-9_-]{16,}"
    "jwt" = "eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    "key-in-url" = "(?i)[?&](?:api[_-]?key|access[_-]?token|auth)=[^&\s]+"
    "email-address" = "(?i)(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
    "korean-resident-number" = "(?<!\d)\d{6}-[1-4]\d{6}(?!\d)"
    "mobile-number" = "(?<!\d)01[016789][ -]\d{3,4}[ -]\d{4}(?!\d)"
    "windows-user-path" = "(?i)[A-Z]:\\Users\\[^\\\s]+"
    "private-workspace-path" = "(?i)D:\\KLab(?:\\|/)"
}

$findings = [System.Collections.Generic.List[object]]::new()
$paths = Get-GitPaths -Selection $Mode

foreach ($path in $paths) {
    $normalized = $path.Replace("\", "/")
    $extension = [IO.Path]::GetExtension($path).ToLowerInvariant()

    $forbiddenPath =
        $normalized -match "^(?:\.agents|\.codex|tmp|output|submission|submissions|knowledge|receipts)/" -or
        $normalized -match "^tools/elice_docs/" -or
        ($normalized -match "^docs/" -and $allowedDocs -notcontains $normalized) -or
        ($forbiddenExtensions -contains $extension) -or
        ($normalized -match "(^|/)\.env(?:\.|$)" -and $normalized -notmatch "\.env\.example$")

    if ($forbiddenPath) {
        $findings.Add([pscustomobject]@{ Path = $normalized; Line = 0; Rule = "forbidden-public-path" })
        continue
    }
    if ($binaryExtensions -contains $extension) {
        continue
    }

    $isConfigLike = $normalized -match '(?i)(^|/)\.env(?:\.|$)|\.(?:json|ya?ml|toml|ini|cfg|properties)$'

    try {
        $lineNumber = 0
        foreach ($line in [IO.File]::ReadLines((Resolve-Path -LiteralPath $path))) {
            $lineNumber++
            foreach ($rule in $contentRules.GetEnumerator()) {
                if ($line -match $rule.Value) {
                    $findings.Add([pscustomobject]@{ Path = $normalized; Line = $lineNumber; Rule = $rule.Key })
                }
            }

            $credentialAssignment = if ($isConfigLike) {
                $line -match '(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*["'']?([^"''\s#]+)'
            }
            else {
                $line -match '(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*=\s*["'']([^"'']+)["'']'
            }
            if (
                $credentialAssignment -and
                $line -notmatch "(?i)synthetic|example|placeholder|test-key|test-only|fake|dummy|change-me|<[^>]+>"
            ) {
                $findings.Add([pscustomobject]@{ Path = $normalized; Line = $lineNumber; Rule = "hardcoded-credential-assignment" })
            }
        }
    }
    catch {
        $findings.Add([pscustomobject]@{ Path = $normalized; Line = 0; Rule = "unreadable-file" })
    }
}

if ($findings.Count -gt 0) {
    Write-Host "Public-tree check failed: $($findings.Count) finding(s). Values are intentionally redacted."
    $findings |
        Sort-Object Path, Line, Rule -Unique |
        Format-Table Path, Line, Rule -AutoSize |
        Out-String |
        Write-Host
    exit 1
}

Write-Host "Public-tree check passed ($Mode): $($paths.Count) file(s), no forbidden paths or sensitive patterns."
