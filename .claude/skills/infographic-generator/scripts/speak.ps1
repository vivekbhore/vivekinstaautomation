# Read a text file aloud into a WAV file, using the voices built into Windows (no network, no keys).
# Used by render_reel.py for the Reel narration.
#   powershell -NoProfile -File speak.ps1 -TextFile in.txt -OutFile out.wav [-Voice "Microsoft Mark"] [-Rate 0]
param(
    [Parameter(Mandatory = $true)][string]$TextFile,
    [Parameter(Mandatory = $true)][string]$OutFile,
    [string]$Voice = "Microsoft Mark",
    [int]$Rate = 0
)
Add-Type -AssemblyName System.Speech
$text = Get-Content -LiteralPath $TextFile -Raw -Encoding UTF8
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $names = $synth.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }
    if ($names -contains $Voice) { $synth.SelectVoice($Voice) }
    else { Write-Output "voice '$Voice' not installed; using the default voice"; }
    $synth.Rate = $Rate
    $synth.SetOutputToWaveFile($OutFile)
    $synth.Speak($text)
}
finally {
    $synth.Dispose()
}
