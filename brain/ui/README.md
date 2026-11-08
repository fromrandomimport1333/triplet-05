# Timeline screen (React + shadcn/ui)

The live screen served by `python -m src.timeline` on http://127.0.0.1:8090.

- React 18, TypeScript, Vite, Tailwind CSS 3, [shadcn/ui](https://ui.shadcn.com) components (`src/components/ui/`, Radix primitives), Inter and JetBrains Mono bundled (no CDN, works offline).
- It only reads the timeline server's routes: `/events` (SSE `row` + `state`), `/api/recent`, `/api/status`, `/api/chain/{cid}`.
- The city map (`src/map/city.ts`) is the previous imperative SVG drawing, mounted by `components/CityMap.tsx`.
- **The build is committed** in `../src/timeline/static/`, so running the Brain needs no Node. Without it the server falls back to the previous single page (`../src/timeline/page.html`).

```bash
cd brain/ui
npm install
npm run build          # type-check + build into ../src/timeline/static/
npm run dev            # hot reload on :5173, proxies /events and /api to a timeline on :8091
                       # (start one without a broker: python -m src.timeline.demo --port 8091)
```

Keys on the screen: `M` switches map/timeline focus, `F` full screen, `Esc` closes the chain dialog.
