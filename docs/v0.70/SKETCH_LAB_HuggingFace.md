# Sketch Lab — HuggingFace Migration Guide

This document is the comprehensive reference for Sketch Lab. It contains everything in the base `SKETCH_LAB.md` reference PLUS a step-by-step guide to swap the Anthropic API out for a vision-language model from HuggingFace. The rendering pipeline, prompt strategy, and frontend stay almost unchanged — only the inference backend gets replaced.

---

## What it is

A weird, experimental drawing tool. Paste an image URL — a vision-capable language model studies it, commits to a structural plan, then animates a 60-stroke line drawing across the canvas in real time using smooth spline-interpolated pencil strokes with tapered width.

The tool is built around a single guiding constraint: **a hard ceiling on stroke count**. Limited budget forces the model to prioritize what matters about the image. The result is somewhere between a portrait, a contour map, and a haiku.

Current version: `v0.70 / DEEP ANALYSIS`

---

## What it does

1. You paste an image URL into the input bar.
2. The frontend sends the URL to a backend (Anthropic API or local VLM) along with a long structured prompt.
3. The model performs a four-phase analysis — observation, structural anchors, stroke plan, execution — and returns a JSON object containing landmark coordinates and up to 60 stroke polylines.
4. The frontend smooths each polyline through a Catmull-Rom spline, then animates the strokes onto a canvas with variable line weight to simulate pencil-on-paper.
5. You watch the drawing form, stroke by stroke, with the current stroke's intent labelled below the status bar.

---

## The four-phase methodology

The single most important design decision in this tool: **the prompt forces structured analysis before any drawing happens**. The model cannot place stroke coordinates without first committing to a skeleton (landmarks) and a script (plan). The JSON schema is the gate.

```
┌─────────────────────────────────────────┐
│ PHASE 1 — OBSERVATION                   │
│ What is in the image? Subject, mood,    │
│ lighting, aspect ratio. 2-3 sentences.  │
└─────────────────────────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ PHASE 2 — STRUCTURAL ANCHORS            │
│ 8-15 landmark points with explicit      │
│ (x,y) coordinates in 0-1000 space.      │
│ "left eye outer corner: 380, 400" etc.  │
│ This is the skeleton.                   │
└─────────────────────────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ PHASE 3 — STROKE PLAN                   │
│ Numbered list of what each of the 60    │
│ strokes will draw, in priority order.   │
│ Budget allocation:                      │
│   1-8:   silhouette + gesture           │
│   9-25:  major forms                    │
│   26-45: secondary forms + features     │
│   46-60: critical fine details          │
└─────────────────────────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ PHASE 4 — EXECUTION                     │
│ The strokes themselves, as polylines.   │
│ Each stroke is anchored to landmarks    │
│ and follows the plan.                   │
└─────────────────────────────────────────┘
```

This routes the work through the model's strengths (structural reasoning) instead of asking it to do precision freestyle (raw coordinate generation in one shot).

---

## The rendering pipeline

```
Image URL
   │
   ▼
Model API call (URL image source — server-side fetch, bypasses browser CORS)
   │
   ▼
JSON parse → strokes[] of control points + landmarks + plan
   │
   ▼
For each stroke:
   ├─→ Apply small random wobble to interior control points
   ├─→ Catmull-Rom spline interpolation (10 samples per segment)
   ├─→ Compute cumulative arc length
   └─→ Animate sub-segment-by-sub-segment with variable line width
            ├─ Taper-on over first 12% of arc length
            ├─ Sinusoidal pressure variation through middle
            └─ Taper-off over last 12%
```

### Catmull-Rom spline (briefly)

The polyline the model returns is treated as **control points**, not the rendered geometry. Each stroke runs through a Catmull-Rom interpolation using phantom endpoints (reflections of the first and last interior points) so the resulting curve passes cleanly through the model's first and last point. Ten interpolated samples per original segment. Wobble is applied to interior control points *before* smoothing, so the curves are organic but never spiky — the spline absorbs jitter into gentle natural variation.

### Pencil rendering

Each smoothed sub-segment is drawn with its own `lineWidth` based on its position along the stroke's arc length. The width profile:

- **Taper-on** over first 12%: `min(1, t/0.12 + 0.25)`
- **Pressure** in the middle: `0.88 + 0.18 * sin(t * π)`
- **Taper-off** over last 12%: `min(1, (1 - t)/0.12 + 0.25)`

Plus per-stroke variation in base width (1.0–1.8px) and opacity (0.72–0.94). Round line caps mean sub-segment joints disappear visually.

---

## Project structure

When porting out of the Claude artifact runtime to a standalone project:

```
sketch-lab/
├── README.md
├── package.json
├── vite.config.js
├── tailwind.config.js
├── postcss.config.js
├── index.html
├── .env
├── .env.example
├── .gitignore
├── src/
│   ├── main.jsx
│   ├── App.jsx
│   ├── SketchLab.jsx        ← the entire tool
│   └── index.css
├── server/                  ← Anthropic proxy (baseline)
│   ├── package.json
│   └── proxy.js
└── server-hf/               ← HuggingFace backend (migration target)
    ├── requirements.txt
    └── main.py
```

You run **either** `server/` or `server-hf/` — the frontend doesn't care which is behind `/api/messages`, as long as something is.

**Why a backend server at all?** In the Claude artifact runtime, `fetch('https://api.anthropic.com/v1/messages')` works without authentication because the runtime injects credentials. Outside that runtime, you must not put your API key in client code (it'd be exposed to anyone who opens devtools). The backend holds credentials (or runs the model) server-side and forwards requests.

---

## Source code

### `src/SketchLab.jsx`

The entire tool is one React component. The only external dependency is React itself (hooks). Tailwind utility classes are used for layout.

```jsx
import { useState, useRef, useEffect } from 'react';

// ---------- Spline smoothing (Catmull-Rom) ----------

function smoothPolyline(points, samplesPerSegment = 10) {
  if (points.length < 2) return points.slice();
  if (points.length === 2) {
    const out = [];
    const N = samplesPerSegment;
    for (let i = 0; i <= N; i++) {
      const t = i / N;
      out.push([
        points[0][0] + (points[1][0] - points[0][0]) * t,
        points[0][1] + (points[1][1] - points[0][1]) * t
      ]);
    }
    return out;
  }
  const n = points.length;
  const ext = [
    [2 * points[0][0] - points[1][0], 2 * points[0][1] - points[1][1]],
    ...points,
    [2 * points[n - 1][0] - points[n - 2][0], 2 * points[n - 1][1] - points[n - 2][1]]
  ];
  const out = [ext[1].slice()];
  const N = samplesPerSegment;
  for (let i = 1; i < ext.length - 2; i++) {
    const p0 = ext[i - 1], p1 = ext[i], p2 = ext[i + 1], p3 = ext[i + 2];
    for (let j = 1; j <= N; j++) {
      const t = j / N;
      const t2 = t * t, t3 = t2 * t;
      const x = 0.5 * (
        2 * p1[0]
        + (-p0[0] + p2[0]) * t
        + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
        + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
      );
      const y = 0.5 * (
        2 * p1[1]
        + (-p0[1] + p2[1]) * t
        + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
        + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
      );
      out.push([x, y]);
    }
  }
  return out;
}

// ---------- API: deep single-pass analysis ----------

const VARIANT_HINTS = [
  '',
  'Emphasize bold silhouette and gesture. Loose and confident.',
  'Emphasize structural geometry and underlying construction.',
  'Focus on unexpected details and negative space.',
  'Draw with naive confidence — bold, simple, childlike.',
  'Exaggerate proportions toward gentle caricature.',
];

const STROKE_FORMAT_BLOCK = `STROKE FORMAT:
- Each stroke is a polyline of 3 to 10 integer (x,y) points in 0-1000 coordinate space
- (0,0) at top-left, y increases downward
- Each stroke includes a "describes" field: short label naming what this stroke represents
- Use MORE points where the line curves significantly (curves are reconstructed via spline interpolation)
- Use FEWER points for straight or nearly-straight lines
- Each stroke = ONE continuous mark. Don't break a single contour into multiple strokes.`;

function buildDeepAnalysisPrompt(hint) {
  return `You are a master line artist with a methodical practice. You will analyze this image with great care across FOUR PHASES, then produce a line drawing of up to 60 strokes.

CRITICAL RULE: You MUST complete each phase in order, in the JSON, before the next. The structured analysis IS the work — do not skip ahead to placing stroke coordinates. The quality of phases 1-3 directly determines the quality of phase 4.

═══ PHASE 1 — OBSERVATION ═══
Look at the image carefully. In 2-3 sentences, describe:
- The subject, setting, and overall mood
- The lighting direction and quality (when relevant for form)
- The image's aspect ratio (judge by eye)
- Anything unusual or distinctive worth capturing

═══ PHASE 2 — STRUCTURAL ANCHORS (CRITICAL) ═══
Identify 8 to 15 LANDMARK POINTS on the primary subject. Each landmark is a specific recognizable feature with explicit (x, y) coordinates in 0-1000 space.

These landmarks are the SKELETON of your drawing. Every stroke you place later MUST be positioned consistently with these landmark coordinates. Spend real cognitive effort here — precise landmark placement is what separates a recognizable likeness from an abstract sketch.

Example landmark types:
- For a face: "top of head", "chin point", "left/right jaw corner", "left/right eye outer corner", "left/right eye inner corner", "nose bridge", "nose tip", "mouth left/right corner", "upper lip peak"
- For a body: "head crown", "neck base", "left/right shoulder", "elbow", "wrist", "hip", "knee", "ankle"
- For an animal: "ear tips", "eye centers", "nose/snout tip", "shoulder peak", "haunch", "paw positions"
- For architecture: "roof peaks", "wall corners", "window centers", "door corners", "foundation corners"
- For objects: structural extremes — corners, peaks, joints, focal features

Be specific to THIS image. Don't use generic positions — measure each landmark by eye and commit to coordinates.

═══ PHASE 3 — STROKE PLAN ═══
BEFORE assigning any stroke coordinates, write out a numbered plan listing what EACH stroke will draw. Allocate the 60-stroke budget approximately:
- Strokes 1–8: silhouette and gesture (large confident outer contours, the big shape)
- Strokes 9–25: major forms (primary masses, head shape, body, prominent features)
- Strokes 26–45: secondary forms and feature placement (features positioned AT your landmark coordinates)
- Strokes 46–60: critical fine details (eyes, distinctive marks, focal elements, signature features)

Each plan entry is a brief description, e.g. "stroke 14: left jawline from temple to chin point".

═══ PHASE 4 — EXECUTION ═══
Only now produce the actual stroke coordinates. Each stroke MUST be consistent with:
1. The landmarks you committed to in Phase 2 (e.g., a stroke drawing the left eye must terminate at the "left eye" landmark coordinates)
2. The plan you wrote in Phase 3 (in order, doing what you said)

${STROKE_FORMAT_BLOCK}

ART PRINCIPLES:
- Impressionistic but COMMITTED. Specific edges, not vague blobs.
- Confident, deliberate marks. No scribbling, no tentative repeats.
- Negative space matters — don't outline everything that exists.
- No color, no shading, no hatching, no cross-hatching.${hint ? '\n- ' + hint : ''}

Respond with ONLY this JSON object. No markdown fences, no prose outside the JSON. The fields must appear in the order shown — observation first, then aspect, then landmarks, then stroke_plan, then strokes.
{
  "observation": "<2-3 sentences from PHASE 1>",
  "aspect": [w, h],
  "landmarks": [
    {"name": "<feature name>", "x": <int 0-1000>, "y": <int 0-1000>}
  ],
  "stroke_plan": [
    "stroke 1: <brief description>",
    "stroke 2: <brief description>"
  ],
  "strokes": [
    {"describes": "<part name>", "points": [[x,y], [x,y], [x,y]]}
  ]
}`;
}

async function callModelForStrokes(imageUrl, userText) {
  const content = [
    { type: "image", source: { type: "url", url: imageUrl } },
    { type: "text", text: userText }
  ];

  // For standalone deployment, point this at your local backend.
  // - Anthropic proxy: "/api/messages" (Vite proxies to localhost:3001)
  // - HuggingFace backend: same "/api/messages" path, different server
  const resp = await fetch("/api/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "claude-sonnet-4-20250514",  // or "qwen2.5-vl-7b" for the HF backend
      max_tokens: 8000,
      messages: [{ role: "user", content }]
    })
  });

  if (!resp.ok) {
    let body = '';
    try { body = await resp.text(); } catch {}
    throw new Error(`API ${resp.status}: ${body.slice(0, 240)}`);
  }

  const data = await resp.json();
  const textBlock = (data.content || []).find(b => b.type === 'text');
  if (!textBlock) throw new Error('No text in API response');
  let txt = textBlock.text.trim();
  txt = txt.replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/, '').trim();

  const start = txt.indexOf('{');
  const end = txt.lastIndexOf('}');
  if (start === -1 || end === -1) throw new Error('No JSON object found');
  txt = txt.slice(start, end + 1);

  let parsed;
  try { parsed = JSON.parse(txt); }
  catch (e) { throw new Error(`Parse error: ${e.message}`); }

  if (!parsed.strokes || !Array.isArray(parsed.strokes)) {
    throw new Error('Response missing strokes array');
  }

  const strokes = parsed.strokes
    .map(s => {
      if (!s || typeof s !== 'object') return null;
      const pts = Array.isArray(s.points) ? s.points : (Array.isArray(s) ? s : null);
      if (!Array.isArray(pts) || pts.length < 2) return null;
      const cleaned = pts.map(pt => Array.isArray(pt) ? [Number(pt[0]) || 0, Number(pt[1]) || 0] : [0, 0]);
      return {
        points: cleaned,
        describes: typeof s.describes === 'string' ? s.describes : ''
      };
    })
    .filter(s => s !== null);

  let aspect = parsed.aspect;
  if (aspect && Array.isArray(aspect) && aspect.length === 2 &&
      Number.isFinite(Number(aspect[0])) && Number.isFinite(Number(aspect[1]))) {
    aspect = [Number(aspect[0]), Number(aspect[1])];
  } else {
    aspect = null;
  }

  const observation = (typeof parsed.observation === 'string' && parsed.observation) ||
                      (typeof parsed.analysis === 'string' && parsed.analysis) || '';

  const landmarks = Array.isArray(parsed.landmarks)
    ? parsed.landmarks
        .filter(l => l && typeof l === 'object' &&
                     typeof l.name === 'string' &&
                     Number.isFinite(Number(l.x)) &&
                     Number.isFinite(Number(l.y)))
        .map(l => ({ name: l.name, x: Number(l.x), y: Number(l.y) }))
    : [];

  const strokePlan = Array.isArray(parsed.stroke_plan)
    ? parsed.stroke_plan.filter(s => typeof s === 'string')
    : [];

  return { strokes, aspect, observation, landmarks, strokePlan };
}

async function fetchDeepDraft(imageUrl, variant) {
  const hint = VARIANT_HINTS[variant % VARIANT_HINTS.length];
  return callModelForStrokes(imageUrl, buildDeepAnalysisPrompt(hint));
}

// ---------- Component ----------

export default function SketchLab() {
  const DEFAULT_URL = 'https://images.unsplash.com/photo-1543466835-00a7907e9de1?w=600';
  const [url, setUrl] = useState(DEFAULT_URL);
  const [status, setStatus] = useState('IDLE');
  const [error, setError] = useState('');
  const [strokeCount, setStrokeCount] = useState(0);
  const [totalStrokes, setTotalStrokes] = useState(60);
  const [canvasW, setCanvasW] = useState(600);
  const [canvasH, setCanvasH] = useState(450);
  const [redrawCount, setRedrawCount] = useState(0);
  const [observation, setObservation] = useState('');
  const [landmarkSummary, setLandmarkSummary] = useState('');
  const [planSummary, setPlanSummary] = useState('');
  const [currentLabel, setCurrentLabel] = useState('');

  const canvasRef = useRef(null);
  const lastUrlRef = useRef(null);
  const runIdRef = useRef(0);

  useEffect(() => { paintPaper(); }, [canvasW, canvasH]);

  function paintPaper() {
    const c = canvasRef.current;
    if (!c) return;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#ece4d3';
    ctx.fillRect(0, 0, c.width, c.height);
    const img = ctx.getImageData(0, 0, c.width, c.height);
    const d = img.data;
    for (let i = 0; i < d.length; i += 4) {
      const n = (Math.random() - 0.5) * 14;
      d[i]     = Math.max(0, Math.min(255, d[i]     + n));
      d[i + 1] = Math.max(0, Math.min(255, d[i + 1] + n));
      d[i + 2] = Math.max(0, Math.min(255, d[i + 2] + n));
    }
    ctx.putImageData(img, 0, 0);
  }

  function animateStroke(ctx, controlPoints, runId) {
    return new Promise(resolve => {
      if (controlPoints.length < 2) { resolve(); return; }
      const wobbled = controlPoints.map((p, i) => {
        if (i === 0 || i === controlPoints.length - 1) return [p[0], p[1]];
        return [p[0] + (Math.random() - 0.5) * 1.4, p[1] + (Math.random() - 0.5) * 1.4];
      });
      const smoothed = smoothPolyline(wobbled, 10);

      let total = 0;
      const cum = [0];
      for (let i = 1; i < smoothed.length; i++) {
        total += Math.hypot(smoothed[i][0] - smoothed[i - 1][0], smoothed[i][1] - smoothed[i - 1][1]);
        cum.push(total);
      }
      if (total < 0.5) { resolve(); return; }

      const duration = Math.min(1300, Math.max(280, total * 4.5));
      const startTime = performance.now();
      const baseWidth = 1.0 + Math.random() * 0.8;
      const alpha = 0.72 + Math.random() * 0.22;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.strokeStyle = `rgba(28, 26, 24, ${alpha})`;

      function widthMult(localT) {
        const startTaper = Math.min(1, localT / 0.12 + 0.25);
        const endTaper = Math.min(1, (1 - localT) / 0.12 + 0.25);
        const taper = Math.min(startTaper, endTaper);
        const pressure = 0.88 + 0.18 * Math.sin(localT * Math.PI);
        return taper * pressure;
      }

      let lastX = smoothed[0][0], lastY = smoothed[0][1];
      let lastIdx = 0;

      function frame(now) {
        if (runId !== runIdRef.current) { resolve(); return; }
        const t = Math.min(1, (now - startTime) / duration);
        const targetLen = total * t;

        let i = lastIdx;
        while (i < smoothed.length - 1 && cum[i + 1] <= targetLen) {
          const midLen = (cum[i] + cum[i + 1]) * 0.5;
          ctx.lineWidth = baseWidth * widthMult(midLen / total);
          ctx.beginPath();
          ctx.moveTo(lastX, lastY);
          ctx.lineTo(smoothed[i + 1][0], smoothed[i + 1][1]);
          ctx.stroke();
          lastX = smoothed[i + 1][0]; lastY = smoothed[i + 1][1];
          i++;
        }

        if (i < smoothed.length - 1 && targetLen < total) {
          const segStart = cum[i], segEnd = cum[i + 1];
          const r = (targetLen - segStart) / Math.max(0.001, segEnd - segStart);
          const x = smoothed[i][0] + (smoothed[i + 1][0] - smoothed[i][0]) * r;
          const y = smoothed[i][1] + (smoothed[i + 1][1] - smoothed[i][1]) * r;
          const midLen = (cum[i] + targetLen) * 0.5;
          ctx.lineWidth = baseWidth * widthMult(midLen / total);
          ctx.beginPath();
          ctx.moveTo(lastX, lastY);
          ctx.lineTo(x, y);
          ctx.stroke();
          lastX = x; lastY = y;
        }

        lastIdx = i;
        if (t < 1) requestAnimationFrame(frame);
        else resolve();
      }
      requestAnimationFrame(frame);
    });
  }

  function sizeCanvasForAspect(aspect) {
    const [aw, ah] = aspect;
    const maxW = 700, maxH = 520;
    const s = Math.min(maxW / aw, maxH / ah);
    const dispW = Math.round(aw * s);
    const dispH = Math.round(ah * s);
    setCanvasW(dispW); setCanvasH(dispH);
    const c = canvasRef.current;
    if (c) {
      c.width = dispW; c.height = dispH;
      paintPaper();
    }
    return { sx: dispW / 1000, sy: dispH / 1000 };
  }

  async function drawStrokeBatch(strokes, sx, sy, offset, runId) {
    const c = canvasRef.current;
    if (!c) return;
    const ctx = c.getContext('2d');

    for (let i = 0; i < strokes.length; i++) {
      if (runId !== runIdRef.current) return;
      setStrokeCount(offset + i + 1);
      setCurrentLabel(strokes[i].describes || '');
      const scaled = strokes[i].points.map(([x, y]) => [x * sx, y * sy]);
      await animateStroke(ctx, scaled, runId);
      await new Promise(r => setTimeout(r, 70));
    }
  }

  async function run(isRedraw) {
    runIdRef.current++;
    const runId = runIdRef.current;
    setError('');
    setStrokeCount(0);
    setCurrentLabel('');
    if (!isRedraw) {
      setObservation('');
      setLandmarkSummary('');
      setPlanSummary('');
    }

    try {
      const targetUrl = url.trim();
      if (!targetUrl) throw new Error('No URL provided');
      const variant = isRedraw ? (redrawCount + 1) : 0;

      setStatus('ANALYZING');
      const result = await fetchDeepDraft(targetUrl, variant);
      if (runId !== runIdRef.current) return;
      if (!result.strokes.length) throw new Error('Model returned no strokes');

      if (isRedraw) setRedrawCount(redrawCount + 1);
      lastUrlRef.current = targetUrl;

      setObservation(result.observation || '');
      if (result.landmarks.length) {
        const previewNames = result.landmarks.map(l => l.name).slice(0, 5).join(', ');
        const more = result.landmarks.length > 5 ? `, +${result.landmarks.length - 5} more` : '';
        setLandmarkSummary(`${result.landmarks.length} anchors: ${previewNames}${more}`);
      } else {
        setLandmarkSummary('(no landmarks returned)');
      }
      if (result.strokePlan.length) {
        setPlanSummary(`${result.strokePlan.length}-stroke plan committed`);
      } else {
        setPlanSummary('(no plan returned)');
      }

      const aspect = result.aspect || [4, 3];
      const { sx, sy } = sizeCanvasForAspect(aspect);

      const strokes = result.strokes.slice(0, 60);
      setTotalStrokes(strokes.length);

      setStatus('DRAWING');
      await drawStrokeBatch(strokes, sx, sy, 0, runId);
      if (runId !== runIdRef.current) return;

      setCurrentLabel('');
      setStatus('COMPLETE');
    } catch (e) {
      setError(e.message || 'something broke');
      setStatus('ERROR');
    }
  }

  function handleDraw() { setRedrawCount(0); run(false); }
  function handleRedraw() { run(true); }

  const pad = (n) => String(n).padStart(2, '0');
  const showRedraw = (status === 'COMPLETE' || status === 'ERROR') && lastUrlRef.current;
  const busy = status === 'ANALYZING' || status === 'DRAWING';
  const dotColor =
    status === 'DRAWING'   ? '#d97706' :
    status === 'ANALYZING' ? '#0ea5e9' :
    status === 'COMPLETE'  ? '#16a34a' :
    status === 'ERROR'     ? '#dc2626' : '#525252';

  return (
    <div className="min-h-screen w-full bg-neutral-950 text-neutral-300 font-mono p-4 sm:p-6 flex flex-col items-center">
      <div className="w-full max-w-3xl">
        <div className="flex items-baseline justify-between mb-1">
          <div className="text-neutral-100 tracking-widest text-sm">SKETCH_LAB</div>
          <div className="text-[10px] text-neutral-500 tracking-widest">v0.70 / DEEP ANALYSIS</div>
        </div>
        <div className="h-px bg-neutral-800 mb-4" />

        <div className="flex flex-col sm:flex-row gap-2 mb-3">
          <div className="flex-1 flex items-center bg-neutral-900 border border-neutral-800 px-2">
            <span className="text-neutral-600 text-xs mr-2 select-none">URL&gt;</span>
            <input
              type="text"
              value={url}
              onChange={e => setUrl(e.target.value)}
              spellCheck={false}
              className="flex-1 bg-transparent text-neutral-200 text-xs py-2 outline-none placeholder-neutral-700"
              placeholder="paste image URL..."
            />
          </div>
          <button
            onClick={handleDraw}
            disabled={busy}
            className="bg-neutral-200 text-neutral-950 text-xs tracking-widest px-4 py-2 hover:bg-white disabled:bg-neutral-700 disabled:text-neutral-500 disabled:cursor-not-allowed transition-colors"
          >
            DRAW
          </button>
        </div>

        <div className="flex items-center justify-between text-[11px] tracking-widest mb-1">
          <div className="flex items-center gap-2">
            <span className="relative flex h-2 w-2">
              {busy && (
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full opacity-75" style={{ backgroundColor: dotColor }} />
              )}
              <span className="relative inline-flex rounded-full h-2 w-2" style={{ backgroundColor: dotColor }} />
            </span>
            <span className="text-neutral-400">STATUS: <span className="text-neutral-100">{status}</span></span>
          </div>
          <div className="text-neutral-400">
            STROKE <span className="text-neutral-100">{pad(strokeCount)}</span> / <span className="text-neutral-100">{pad(totalStrokes)}</span>
          </div>
        </div>

        <div className="h-4 mb-2">
          {currentLabel && (status === 'DRAWING') && (
            <div className="text-[10px] text-neutral-500 italic truncate">
              → drawing: "{currentLabel}"
            </div>
          )}
        </div>

        <div className="bg-neutral-900 border border-neutral-800 p-3 flex items-center justify-center">
          <canvas
            ref={canvasRef}
            width={canvasW}
            height={canvasH}
            className="max-w-full h-auto shadow-2xl"
          />
        </div>

        {(observation || landmarkSummary || planSummary) && (
          <div className="mt-3 text-[11px] text-neutral-400 border-l-2 border-neutral-700 bg-neutral-900/50 px-3 py-2 leading-relaxed space-y-1">
            {observation && (
              <div>
                <span className="text-neutral-600 tracking-widest">[OBS] </span>
                <span className="text-neutral-300 italic">{observation}</span>
              </div>
            )}
            {landmarkSummary && (
              <div>
                <span className="text-neutral-600 tracking-widest">[ANCHORS] </span>
                <span className="text-neutral-400">{landmarkSummary}</span>
              </div>
            )}
            {planSummary && (
              <div>
                <span className="text-neutral-600 tracking-widest">[PLAN] </span>
                <span className="text-neutral-400">{planSummary}</span>
              </div>
            )}
          </div>
        )}

        {error && (
          <div className="mt-3 text-[11px] text-red-400 border border-red-900/40 bg-red-950/20 px-3 py-2 break-all">
            ERR &gt; {error}
          </div>
        )}

        <div className="mt-3 flex items-center justify-between gap-3">
          <div className="text-[10px] text-neutral-600 tracking-wider leading-relaxed flex-1">
            // four-phase pipeline: OBSERVATION → ANCHORS → PLAN → EXECUTION
            <br />// landmarks lock spatial positions before any stroke is placed.
            <br />// REDRAW = same image, different artistic angle, full reanalysis.
          </div>
          {showRedraw && (
            <button
              onClick={handleRedraw}
              disabled={busy}
              className="bg-transparent border border-neutral-700 text-neutral-300 text-xs tracking-widest px-3 py-2 hover:border-neutral-400 hover:text-neutral-100 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              ↻ REDRAW
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
```

### `server/proxy.js` (Anthropic baseline)

A minimal Node + Express proxy. Holds the Anthropic API key server-side; the frontend talks to it instead of directly to api.anthropic.com.

```js
import express from 'express';
import cors from 'cors';
import 'dotenv/config';

const app = express();
app.use(express.json({ limit: '20mb' }));
app.use(cors());

const API_KEY = process.env.ANTHROPIC_API_KEY;
if (!API_KEY) {
  console.error('ANTHROPIC_API_KEY missing from environment');
  process.exit(1);
}

app.post('/api/messages', async (req, res) => {
  try {
    const response = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': API_KEY,
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify(req.body),
    });
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => console.log(`Proxy listening on :${PORT}`));
```

### `server/package.json`

```json
{
  "name": "sketch-lab-proxy",
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "start": "node proxy.js",
    "dev": "node --watch proxy.js"
  },
  "dependencies": {
    "cors": "^2.8.5",
    "dotenv": "^16.4.5",
    "express": "^4.19.2"
  }
}
```

### `package.json` (frontend)

```json
{
  "name": "sketch-lab",
  "private": true,
  "version": "0.7.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.1",
    "autoprefixer": "^10.4.20",
    "postcss": "^8.4.47",
    "tailwindcss": "^3.4.13",
    "vite": "^5.4.8"
  }
}
```

### `vite.config.js`

```js
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:3001',
        changeOrigin: true,
      }
    }
  }
});
```

With this proxy config, the frontend can call `/api/messages` and Vite will forward to whichever backend (Node proxy or Python FastAPI) is running on port 3001 during dev.

### `tailwind.config.js`

```js
export default {
  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],
  theme: { extend: {} },
  plugins: [],
};
```

### `postcss.config.js`

```js
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
```

### `src/index.css`

```css
@tailwind base;
@tailwind components;
@tailwind utilities;
```

### `src/main.jsx`

```jsx
import React from 'react';
import ReactDOM from 'react-dom/client';
import SketchLab from './SketchLab.jsx';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <SketchLab />
  </React.StrictMode>
);
```

### `index.html`

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Sketch Lab</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

### `.env.example`

```
ANTHROPIC_API_KEY=sk-ant-...
```

Copy to `.env` and fill in your key. Do not commit `.env`.

### `.gitignore`

```
node_modules
.env
.venv
dist
.DS_Store
__pycache__
```

---

## Setup & run (baseline / Anthropic)

**One-time setup:**

```bash
git init sketch-lab && cd sketch-lab
# create the file structure above, paste in each file's contents

# Install frontend deps
npm install

# Install proxy deps
cd server && npm install && cd ..

# Configure your API key
cp .env.example .env
# edit .env, paste your Anthropic API key
cp .env server/.env
```

**Run (two terminals):**

```bash
# Terminal 1 — proxy
cd server && npm run dev

# Terminal 2 — frontend
npm run dev
```

Open the Vite URL (usually `http://localhost:5173`).

---

## Configuration knobs

The main tunables, ranked by impact:

| Lever | Where | Effect |
|---|---|---|
| `max_tokens` in `callModelForStrokes` | API call body | Cap on response size. 8000 is current. |
| Stroke ceiling | `slice(0, 60)` in `run` | Total stroke budget. Lower = more abstract, higher = more detail. |
| Phase 3 budget allocation | prompt text | How strokes are distributed between gesture/forms/details. |
| `samplesPerSegment` | `smoothPolyline` call | Spline density. 10 is current. |
| Wobble magnitude | `animateStroke` | `* 1.4` controls jitter. |
| `widthMult` taper window | `animateStroke` | `0.12` controls the lift-on/lift-off fraction. |
| Base width range | `animateStroke` | `1.0 + Math.random() * 0.8`. |
| Alpha range | `animateStroke` | `0.72 + Math.random() * 0.22`. |
| `duration` per stroke | `animateStroke` | Pacing of the pencil. |
| Inter-stroke delay | `drawStrokeBatch` | Pause between strokes. |
| Paper color | `paintPaper` | `#ece4d3` (cream). |
| Stroke color | `animateStroke` | `rgba(28, 26, 24, ...)` (graphite). |
| Variant hints | `VARIANT_HINTS` array | Stylistic alternates triggered by REDRAW. |

---

## Known limitations

- **The model can't measure.** Coordinates are estimates from a semantic understanding of the image. Spatial precision will never match an edge detector. Expect impressionistic accuracy, not photographic.
- **JSON parsing is sometimes brittle.** The parser tries to extract the JSON object between the first `{` and last `}`. Mostly works, occasionally fails.
- **No multi-image support.** The earlier two-pass refinement approach was removed because the model couldn't reliably align coordinates between passes.
- **No caching of analysis.** REDRAW re-runs the full pipeline. Add a "use last response" toggle if you want to tweak rendering without re-querying.
- **CORS / image accessibility.** The backend fetches the image server-side, so browser CORS doesn't apply. URLs still need to be publicly accessible.

---

# ════════════════════════════════════════════════════════════
# HUGGINGFACE MIGRATION GUIDE
# ════════════════════════════════════════════════════════════

Everything below replaces the Anthropic backend with a local vision-language model loaded via HuggingFace `transformers`. The frontend changes are negligible.

---

## Why migrate

- **Cost.** Each API call is several thousand tokens out plus an image; at scale this adds up. A local model is amortized.
- **Latency control.** Local inference can be faster than API round-trips, especially with vLLM.
- **Privacy / sovereignty.** Reference images and outputs never leave your machine.
- **Fine-tuning.** You can train the model on your own taste (image + ideal stroke JSON pairs) once you've built up a dataset.
- **Determinism.** Local seeds give reproducible runs.

Tradeoffs:

- **Setup complexity.** A Python backend with a heavy ML stack.
- **Quality ceiling depends on the model.** Frontier open VLMs are good but Sonnet-class quality on this specific task is still slightly out of reach as of writing. Expect rougher landmarks and less reliable JSON conformance.
- **VRAM.** Even a "small" 7B VLM wants ~16 GB in FP16, and you want headroom.

---

## Architecture comparison

**Anthropic (baseline) flow:**

```
[React] ──HTTP──▶ [Node proxy :3001] ──HTTPS──▶ [api.anthropic.com] ──▶ Claude
   ▲                                                                       │
   └───────────────── JSON ←────────────────────────────────────────────────┘
```

**HuggingFace flow:**

```
[React] ──HTTP──▶ [Python FastAPI :3001] ──in-process──▶ [Qwen2.5-VL on GPU]
   ▲                                                              │
   └───────────────── JSON ←──────────────────────────────────────┘
```

The Python backend takes over the role of both the proxy and the inference provider. It:

1. Accepts a request shape similar to Anthropic's (image URL + text prompt).
2. Fetches the image bytes server-side.
3. Runs the VLM via `transformers`.
4. Parses the model's output as JSON.
5. Returns the same response shape the React app already knows how to consume.

Because both backends listen on `:3001` and expose `POST /api/messages` with compatible request/response shapes, the frontend doesn't care which is running.

---

## Recommended model

**Default recommendation: `Qwen/Qwen2.5-VL-7B-Instruct`**

Reasons:
- Strong on chart/document understanding, which transfers reasonably well to "produce coordinates referencing visual landmarks."
- Native support in `transformers` (well-maintained processor + model classes).
- 7B is comfortable on a 24 GB consumer card, trivial on higher-end hardware.
- Apache 2.0 license.
- Tokenizer handles images via the standard chat template.

**Stronger alternatives** (need more VRAM):

- `Qwen/Qwen2.5-VL-32B-Instruct` — meaningful quality bump. ~64 GB in FP16, ~17 GB in 4-bit AWQ.
- `Qwen/Qwen2.5-VL-72B-Instruct` — top open VLM as of writing. ~140 GB FP16 (won't fit on a single card), ~36 GB in 4-bit AWQ (fits on a 48 GB card).
- `OpenGVLab/InternVL2_5-78B` — competitive alternative.
- `meta-llama/Llama-3.2-90B-Vision-Instruct` — Meta's VLM, behind Qwen on structured output in our experience.

**For experimenting**: start with the 7B, get the pipeline working, then swap up to 32B or 72B and re-evaluate output quality. The code doesn't change.

---

## Hardware requirements

For `Qwen2.5-VL-7B-Instruct`:

| Setting | VRAM | Latency (single image, ~6k tokens out) |
|---|---|---|
| FP16, eager attention | ~16 GB | ~25–40 s per request |
| FP16, flash-attention-2 | ~14 GB | ~15–25 s |
| AWQ 4-bit | ~7 GB | ~10–20 s |

For 32B in 4-bit, expect roughly double the latency above. For 72B in 4-bit, triple to quadruple.

A modern Blackwell-class GPU with ≥48 GB VRAM runs anything in this range comfortably; the 96 GB tier opens 72B-class models in 4-bit.

---

## Step 1 — Python environment

Create the backend directory and set up a venv:

```bash
mkdir server-hf && cd server-hf
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

`server-hf/requirements.txt`:

```
torch>=2.4
transformers>=4.49
accelerate>=0.34
qwen-vl-utils>=0.0.10
pillow>=10.0
requests>=2.32
fastapi>=0.110
uvicorn[standard]>=0.30
python-dotenv>=1.0
# Optional acceleration:
# flash-attn>=2.6   # only if your CUDA/PyTorch combo supports it
# autoawq>=0.2     # if loading AWQ-quantized checkpoints
```

Install:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

For PyTorch with CUDA matched to your driver, follow https://pytorch.org/get-started/locally/. On Blackwell (sm_120) GPUs you may need a nightly PyTorch build with CUDA 12.8+.

---

## Step 2 — Backend implementation

### `server-hf/main.py`

```python
"""
Sketch Lab — HuggingFace backend.

Exposes a single POST /api/messages endpoint that mimics the subset of the
Anthropic Messages API the frontend uses. Routes inference to a local
Qwen2.5-VL model.
"""

import os
import io
import json
import re
import base64
import requests
from typing import Any
from contextlib import asynccontextmanager

import torch
from PIL import Image
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from transformers import AutoProcessor, AutoModelForImageTextToText
from qwen_vl_utils import process_vision_info

MODEL_ID = os.environ.get("MODEL_ID", "Qwen/Qwen2.5-VL-7B-Instruct")
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DTYPE = torch.bfloat16 if DEVICE == "cuda" else torch.float32

# Load model + processor once at startup.
state: dict[str, Any] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"Loading {MODEL_ID} on {DEVICE} ({DTYPE})...")
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = AutoModelForImageTextToText.from_pretrained(
        MODEL_ID,
        torch_dtype=DTYPE,
        device_map="auto",
        # attn_implementation="flash_attention_2",  # enable if flash-attn installed
    )
    model.eval()
    state["processor"] = processor
    state["model"] = model
    print("Model loaded.")
    yield
    state.clear()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class MessagesRequest(BaseModel):
    model: str | None = None
    max_tokens: int = 8000
    messages: list[dict]


def fetch_image(url: str) -> Image.Image:
    if url.startswith("data:"):
        header, b64 = url.split(",", 1)
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    r = requests.get(url, timeout=30, headers={"User-Agent": "sketch-lab/0.7"})
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def extract_image_url_and_text(messages: list[dict]) -> tuple[Image.Image, str]:
    """Pull the first image source and the text prompt out of Anthropic-shaped content."""
    if not messages:
        raise ValueError("No messages")
    content = messages[0].get("content", [])
    image: Image.Image | None = None
    text_parts: list[str] = []
    for block in content:
        btype = block.get("type")
        if btype == "image":
            src = block.get("source", {})
            stype = src.get("type")
            if stype == "url":
                image = fetch_image(src["url"])
            elif stype == "base64":
                data = base64.b64decode(src["data"])
                image = Image.open(io.BytesIO(data)).convert("RGB")
        elif btype == "text":
            text_parts.append(block.get("text", ""))
    if image is None:
        raise ValueError("No image found in message")
    return image, "\n\n".join(text_parts)


def build_qwen_messages(image: Image.Image, user_text: str) -> list[dict]:
    """Qwen2.5-VL expects messages in this shape for its chat template."""
    return [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": user_text},
            ],
        }
    ]


def run_inference(image: Image.Image, user_text: str, max_new_tokens: int) -> str:
    processor = state["processor"]
    model = state["model"]

    messages = build_qwen_messages(image, user_text)
    text = processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    image_inputs, video_inputs = process_vision_info(messages)
    inputs = processor(
        text=[text],
        images=image_inputs,
        videos=video_inputs,
        padding=True,
        return_tensors="pt",
    ).to(DEVICE)

    with torch.inference_mode():
        out_ids = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,            # deterministic
            temperature=1.0,
            repetition_penalty=1.05,
        )
    # Trim prompt tokens off the front
    trimmed = [o[len(i):] for i, o in zip(inputs.input_ids, out_ids)]
    output_text = processor.batch_decode(
        trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )[0]
    return output_text


JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    match = JSON_OBJ_RE.search(cleaned)
    if not match:
        raise ValueError(f"No JSON object found in model output: {cleaned[:200]}...")
    return json.loads(match.group(0))


def to_anthropic_shape(parsed_json: dict) -> dict:
    """Wrap the parsed JSON in the response shape the frontend expects:
       { content: [{ type: 'text', text: '<json>' }] }
    """
    return {
        "id": "hf-local",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "text", "text": json.dumps(parsed_json)},
        ],
        "model": MODEL_ID,
        "stop_reason": "end_turn",
    }


@app.post("/api/messages")
def messages_endpoint(req: MessagesRequest):
    try:
        image, user_text = extract_image_url_and_text(req.messages)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad request: {e}")

    # Retry up to 3 times if JSON parsing fails.
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            raw = run_inference(image, user_text, max_new_tokens=req.max_tokens)
            parsed = extract_json(raw)
            return to_anthropic_shape(parsed)
        except Exception as e:
            last_err = e
            print(f"Attempt {attempt + 1} failed: {e}")

    raise HTTPException(status_code=502, detail=f"Inference failed: {last_err}")


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": MODEL_ID}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3001)
```

### Run the backend

```bash
cd server-hf
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 3001
```

First start downloads the model (~15 GB for 7B). Subsequent starts are quick.

To use a bigger model:

```bash
MODEL_ID=Qwen/Qwen2.5-VL-32B-Instruct uvicorn main:app --port 3001
```

To use an AWQ-quantized variant (much less VRAM):

```bash
MODEL_ID=Qwen/Qwen2.5-VL-72B-Instruct-AWQ uvicorn main:app --port 3001
```

---

## Step 3 — Frontend changes

The frontend code in `SketchLab.jsx` already uses `/api/messages` as its fetch URL (see the source listed above). With the Vite proxy in `vite.config.js`, that path is forwarded to whatever is running on `localhost:3001`. So when you swap from the Node Anthropic proxy to the Python FastAPI HF backend, **no frontend code changes are needed** — just stop the Node server and start the Python server.

Optionally update the model string in the request body for log clarity:

```jsx
body: JSON.stringify({
  model: "qwen2.5-vl-7b",   // backend ignores this, just for your own logs
  max_tokens: 8000,
  messages: [{ role: "user", content }]
})
```

---

## Step 4 — Prompt considerations for VLMs

The prompt from `buildDeepAnalysisPrompt` works on Qwen with no changes — it's pure English. A few notes on what to watch:

- **JSON conformance is weaker.** Qwen sometimes wraps its response in markdown fences or prefaces with "Here is the JSON:". The `extract_json` function in `main.py` handles both. The retry-3-times loop catches the rest.
- **Coordinate precision.** Open VLMs are generally less reliable than Claude at producing meaningful (x, y) coordinates. Landmarks are still helpful (they force structure) but expect more drift.
- **Phase 3 stroke plans get long.** Some models try to truncate. If you see only 10–20 strokes coming back when you asked for 60, bump `max_new_tokens` higher and/or shorten the plan instruction (drop "describes" for every plan entry, just number them).

If quality is consistently off, two reliable upgrades:

1. **Bigger model.** 32B and 72B handle structured output noticeably better than 7B.
2. **Constrained decoding.** See Step 6.

---

## Step 5 — Production-grade serving with vLLM (optional)

For sustained use (multiple users, low latency), replace `transformers` with `vLLM`. The throughput improvement is dramatic — typically 5–20× faster.

Install:

```bash
pip install vllm
```

Run vLLM as a standalone server that exposes an OpenAI-compatible API:

```bash
vllm serve Qwen/Qwen2.5-VL-7B-Instruct \
  --host 0.0.0.0 \
  --port 8000 \
  --dtype bfloat16 \
  --limit-mm-per-prompt image=1
```

Then your FastAPI backend doesn't need to load the model — it just forwards to vLLM:

```python
import requests

def run_inference(image: Image.Image, user_text: str, max_new_tokens: int) -> str:
    # Encode image to base64 for OpenAI-style payload
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    payload = {
        "model": "Qwen/Qwen2.5-VL-7B-Instruct",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": user_text},
            ],
        }],
        "max_tokens": max_new_tokens,
        "temperature": 0,
    }
    r = requests.post("http://localhost:8000/v1/chat/completions",
                      json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
```

The rest of `main.py` stays identical. You now have a two-tier setup: FastAPI (translation + retries) talks to vLLM (raw inference). Decoupled, scalable.

---

## Step 6 — Constrained JSON decoding (optional)

If JSON parse failures get annoying, you can force the model to produce valid JSON matching your schema. Two approaches:

### Outlines (works with `transformers` directly)

```bash
pip install outlines
```

Define the schema with Pydantic and let Outlines guide generation:

```python
from pydantic import BaseModel
from typing import List
import outlines

class Landmark(BaseModel):
    name: str
    x: int
    y: int

class Stroke(BaseModel):
    describes: str
    points: List[List[int]]   # list of [x, y]

class DrawingResponse(BaseModel):
    observation: str
    aspect: List[int]
    landmarks: List[Landmark]
    stroke_plan: List[str]
    strokes: List[Stroke]

# In your inference function:
model = outlines.models.transformers(MODEL_ID, device=DEVICE)
generator = outlines.generate.json(model, DrawingResponse)
result = generator(prompt_string)  # returns a DrawingResponse instance
```

The model is constrained at generation time to only output tokens that maintain valid JSON conforming to the schema. No parse failures, ever.

### vLLM guided decoding

vLLM has built-in support via the `extra_body` parameter:

```python
payload["extra_body"] = {
    "guided_json": DrawingResponse.model_json_schema()
}
```

Both approaches add a small generation overhead (~10–20% slower) in exchange for 100% parse success. Worth it.

---

## Step 7 — Fine-tuning on your taste (optional, longer term)

Once the pipeline is running, every successful generation produces a `(image, json_response)` pair. After a few hundred runs you've got a dataset of "Sketch Lab style" outputs.

Workflow:

1. **Collect.** Log every generation to a JSONL file: `{"image_url": "...", "response": {...}}`.
2. **Curate.** Manually review and keep only the ones that produced drawings you actually like.
3. **Reformat.** Convert to Qwen's chat-format training format with the four-phase prompt as input and the JSON as the assistant turn.
4. **Train.** LoRA fine-tune with PEFT on a base Qwen2.5-VL. A few hundred examples is enough to bias the model toward your style.

```bash
pip install peft trl bitsandbytes
```

Skeleton training script (`tune.py`):

```python
from peft import LoraConfig
from trl import SFTTrainer, SFTConfig
from transformers import AutoProcessor, AutoModelForImageTextToText
from datasets import load_dataset

MODEL_ID = "Qwen/Qwen2.5-VL-7B-Instruct"
processor = AutoProcessor.from_pretrained(MODEL_ID)
model = AutoModelForImageTextToText.from_pretrained(
    MODEL_ID, torch_dtype="bfloat16", device_map="auto"
)

lora_config = LoraConfig(
    r=16, lora_alpha=32, lora_dropout=0.05,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    task_type="CAUSAL_LM",
)

dataset = load_dataset("json", data_files="sketch_lab_curated.jsonl", split="train")

training_args = SFTConfig(
    output_dir="./sketch-lab-tune",
    num_train_epochs=3,
    per_device_train_batch_size=1,
    gradient_accumulation_steps=8,
    learning_rate=2e-4,
    bf16=True,
    logging_steps=10,
    save_steps=100,
)

trainer = SFTTrainer(
    model=model,
    args=training_args,
    train_dataset=dataset,
    peft_config=lora_config,
    tokenizer=processor.tokenizer,
)
trainer.train()
trainer.save_model("./sketch-lab-lora")
```

Then load the LoRA on top of the base model at inference time:

```python
from peft import PeftModel
base = AutoModelForImageTextToText.from_pretrained(MODEL_ID, ...)
model = PeftModel.from_pretrained(base, "./sketch-lab-lora")
```

This is the path to a tool that has its own voice rather than borrowing one.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Backend hangs on first request | Model still loading | Wait for `Model loaded.` in logs before sending requests |
| `CUDA out of memory` | Too-large model for VRAM | Use AWQ variant, or smaller model, or `device_map="auto"` with CPU offload |
| `ImportError: flash_attn` | Optional dep not installed | Remove `attn_implementation="flash_attention_2"` from `from_pretrained` call |
| "No JSON object found" | Model rambled | Lower `temperature`, raise `max_new_tokens`, or add constrained decoding (Step 6) |
| Strokes look chaotic | Model not respecting structure | Try a larger model; small VLMs struggle with deep schemas |
| `Bad request: No image found` | Frontend isn't sending an image block | Verify the fetch payload still includes the `{type: "image", source: ...}` block |
| Drawing time too long | Inference latency on 32B+ models | Move to vLLM (Step 5) or use 7B/AWQ variant |
| `sm_120 not supported` (Blackwell) | PyTorch CUDA mismatch | Install PyTorch nightly with CUDA 12.8+ matched to your driver |

---

## Switching between backends

The frontend is backend-agnostic — both implementations expose the same `POST /api/messages` shape on `localhost:3001`. To swap:

```bash
# Use Anthropic
cd server && npm run dev

# Or use HuggingFace
cd server-hf && uvicorn main:app --port 3001
```

You can keep both directories and switch as needed. Only one can listen on `:3001` at a time, but the frontend doesn't care which.

---

## Final file checklist

```
sketch-lab/
├── README.md
├── package.json
├── vite.config.js
├── tailwind.config.js
├── postcss.config.js
├── index.html
├── .env
├── .env.example
├── .gitignore
├── src/
│   ├── main.jsx
│   ├── SketchLab.jsx
│   └── index.css
├── server/                  ← Anthropic proxy
│   ├── package.json
│   ├── .env
│   └── proxy.js
└── server-hf/               ← HuggingFace backend
    ├── requirements.txt
    ├── .venv/
    └── main.py
```

Run **either** `server/` or `server-hf/` — the frontend doesn't care which is behind `/api/messages`, as long as something is.

---

End of Sketch Lab documentation.
