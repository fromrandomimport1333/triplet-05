param([string]$In, [string]$Out)
# Export a .pptx to PDF with PowerPoint (ppSaveAsPDF = 32), without showing a window.
$app = New-Object -ComObject PowerPoint.Application
try {
  $pres = $app.Presentations.Open((Resolve-Path $In).Path, $true, $false, $false)
  $pres.SaveAs($Out, 32)
  $pres.Close()
  "exported $Out"
} finally {
  $app.Quit()
  [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null
}
