param([string]$Path)
# Prints the page count as actually rendered by Word: "PAGES=<n>" (called by validate.py)
$w = New-Object -ComObject Word.Application
$w.Visible = $false
try {
  $d = $w.Documents.Open($Path, $false, $true)
  $d.Repaginate()
  "PAGES=" + $d.ComputeStatistics(2)
  $d.Close($false)
} finally { $w.Quit() }
