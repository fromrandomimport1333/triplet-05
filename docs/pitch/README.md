# Pitch deck

`../pitch.pdf` is the deliverable (7 slides for the 2-minute pitch; speaker notes in the `.pptx`).

| File | Content |
|---|---|
| `pitch.pptx` | Editable deck |
| `live-map.png` | The live screen during scenario B5 (slide 6), captured from the running stack |
| `build.js` | Generates `pitch.pptx` with pptxgenjs (`npm install pptxgenjs react react-dom react-icons sharp`, then `node build.js pitch.pptx`). The numbers on the slides are at the top of the file, in `N` |
| `topdf.ps1` | Exports the `.pptx` to PDF with PowerPoint: `powershell -File topdf.ps1 -In pitch.pptx -Out <absolute path>\pitch.pdf` |

Every number on the slides is measured: scenario results in `logs/`, test count from
`python -m pytest` in `brain/`, LLM figures from `python -m src.llm.bench` (docs/report/part2-city-brain.md).
