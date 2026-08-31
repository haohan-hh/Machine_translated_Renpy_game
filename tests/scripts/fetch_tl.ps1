$ErrorActionPreference = "Stop"
$base = "https://raw.githubusercontent.com/renpy/renpy/master/the_question/game/tl/schinese"
$out = "e:/Code_Buddy_作品/翻译/tests/the_question/tl"
New-Item -ItemType Directory -Force -Path $out | Out-Null
foreach ($f in @("script.rpy", "options.rpy")) {
    $api = curl.exe -sL "https://api.github.com/repos/renpy/renpy/contents/the_question/game/tl/schinese/$f"
    $obj = $api | ConvertFrom-Json
    if ($obj.encoding -eq "base64") {
        [System.IO.File]::WriteAllBytes("$out/$f", [System.Convert]::FromBase64String($obj.content))
        Write-Host "saved tl/$f"
    } else {
        Write-Host "skip tl/$f"
    }
}
