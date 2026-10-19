import { app } from "../../scripts/app.js"
import { api } from "../../scripts/api.js"

// The panel for `LoadVideoTrim`: a scrubbable preview of the chosen file, the
// frame range and crop box as draggable overlays, and the loop assist, which
// asks the server for frames that look like the one under the playhead.
//
// Everything the Python node reads is a plain widget (`file`, `start_frame`,
// `end_frame`, `crop_*`); the panel only drives them and reads them back, so
// typing a number into a widget and dragging a handle are the same operation.
// Panel-only state (playhead, search results, search settings) is kept in
// `node.properties.loop_assist`, which the workflow serializes but the prompt
// never sends.
//
// The file is previewed with a <video> element pointed at `/view`, which is
// what plays natively; a container the browser cannot decode (MKV, AVI...)
// fails that element's load, and the panel then shows server-rendered frames
// instead, with stepping but no playback. Frame numbers come from the server's
// index (`/loadgifasvideo/video/info`) and never from `video.duration`, which
// is approximate; the start/end thumbnails are server frames too, so what they
// show is exactly what the node will cut.

const NODE_CLASS = "LoadVideoTrim"
const PROP = "loop_assist"
const ROUTE = "/loadgifasvideo/video"
const ACCENT = "#38bdf8"
const MATCH = "#f59e0b"
const MIN_WIDTH = 560
const PANEL_MIN_HEIGHT = 470
const THUMB_SIZE = 256
const PREVIEW_SIZE = 768
const HANDLE_GRAB = 8 // px either side of a range handle that grabs it

const ASPECTS = [
  ["Free", 0],
  ["Original", -1],
  ["1:1", 1],
  ["4:3", 4 / 3],
  ["3:4", 3 / 4],
  ["16:9", 16 / 9],
  ["9:16", 9 / 16],
  ["3:2", 3 / 2],
  ["2:3", 2 / 3],
  ["21:9", 21 / 9],
]

const STYLE = `
.lgav-root { display:flex; flex-direction:column; gap:5px; height:100%; box-sizing:border-box;
  padding:6px; font:12px/1.3 sans-serif; color:#ddd; background:#1b1b1b; border-radius:6px;
  overflow:hidden; outline:none; user-select:none; }
.lgav-root:focus-within { box-shadow: inset 0 0 0 1px ${ACCENT}55; }
.lgav-row { display:flex; align-items:center; gap:5px; flex-wrap:wrap; min-height:22px; }
.lgav-row .lgav-grow { flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.lgav-root button { background:#2e2e2e; color:#ddd; border:1px solid #444; border-radius:4px;
  padding:2px 7px; font:inherit; cursor:pointer; line-height:16px; white-space:nowrap; }
.lgav-root button:hover { background:#3a3a3a; }
.lgav-root button:disabled { opacity:.45; cursor:default; }
.lgav-root button.lgav-on { background:${ACCENT}; color:#000; border-color:${ACCENT}; }
.lgav-root input, .lgav-root select { background:#111; color:#ddd; border:1px solid #444; border-radius:3px;
  font:inherit; padding:1px 4px; height:20px; box-sizing:border-box; }
.lgav-root input[type=number] { width:62px; text-align:right; }
.lgav-root input.lgav-small { width:48px; }
.lgav-muted { color:#888; }
.lgav-accent { color:${ACCENT}; }
.lgav-error { color:#ff6b6b; }
.lgav-preview { position:relative; flex:1 1 0; min-height:100px; background:#000; border-radius:4px;
  overflow:hidden; display:flex; align-items:center; justify-content:center; }
.lgav-preview video, .lgav-preview img.lgav-still { max-width:100%; max-height:100%; width:100%; height:100%;
  object-fit:contain; display:block; }
.lgav-drop { position:absolute; inset:0; display:none; align-items:center; justify-content:center;
  background:${ACCENT}22; border:2px dashed ${ACCENT}; color:#fff; font-size:14px; pointer-events:none; }
.lgav-empty { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
  color:#777; text-align:center; padding:12px; pointer-events:none; }
.lgav-crop { position:absolute; border:1px dashed ${ACCENT}; box-sizing:border-box; cursor:move;
  box-shadow:0 0 0 9999px rgba(0,0,0,.55); }
.lgav-crop .lgav-grid { position:absolute; pointer-events:none; border-color:rgba(255,255,255,.25); border-style:dashed; border-width:0; }
.lgav-handle { position:absolute; width:14px; height:14px; box-sizing:border-box; border:0 solid ${ACCENT}; }
.lgav-timeline { display:flex; flex-direction:column; gap:2px; }
.lgav-ruler { position:relative; height:12px; font-size:10px; color:#999; }
.lgav-ruler span { position:absolute; top:0; transform:translateX(-50%); white-space:nowrap; }
.lgav-ruler span:first-child { transform:none; }
.lgav-ruler span:last-child { transform:translateX(-100%); }
.lgav-track { position:relative; height:28px; background:#0f0f0f; border-radius:4px; cursor:pointer;
  box-shadow:inset 0 1px 3px rgba(0,0,0,.6); overflow:hidden; touch-action:none; }
.lgav-fill { position:absolute; top:0; bottom:0; background:${ACCENT}33; pointer-events:none; }
.lgav-marker { position:absolute; top:0; bottom:0; width:2px; margin-left:-1px; background:${MATCH}; opacity:.85; pointer-events:none; }
.lgav-marker.lgav-query { background:#fff; }
.lgav-rhandle { position:absolute; top:0; bottom:0; width:6px; margin-left:-3px; background:${ACCENT}; pointer-events:none;
  box-shadow:0 0 3px #000; }
.lgav-playhead { position:absolute; top:0; bottom:0; width:2px; margin-left:-1px; background:#ff4d4d; pointer-events:none; }
.lgav-playhead::before { content:""; position:absolute; top:0; left:-4px; border:5px solid transparent; border-top-color:#ff4d4d; }
.lgav-results { max-height:112px; min-height:22px; overflow-y:auto; background:#111; border:1px solid #333; border-radius:4px;
  padding:2px; display:flex; flex-direction:column; gap:1px; }
.lgav-res { display:flex; align-items:center; gap:6px; padding:1px 4px; border-radius:3px; cursor:pointer; }
.lgav-res:hover { background:#222; }
.lgav-res.lgav-sel { background:${ACCENT}33; }
.lgav-res.lgav-qrow { border-left:3px solid #fff; padding-left:3px; margin-bottom:2px; }
.lgav-res.lgav-qrow .lgav-accent { color:#fff; }
.lgav-res .lgav-grow { flex:1; }
.lgav-res button { padding:0 5px; line-height:15px; }
.lgav-thumbs { display:flex; gap:6px; }
.lgav-thumb { flex:1 1 0; min-width:0; display:flex; flex-direction:column; gap:2px; align-items:stretch; }
.lgav-thumb .lgav-img { height:80px; background:#000; border-radius:3px; display:flex; align-items:center; justify-content:center; overflow:hidden; }
.lgav-thumb img { max-width:100%; max-height:100%; object-fit:contain; }
.lgav-thumb .lgav-cap { font-size:11px; text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
`

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag)
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v
    else if (k === "style") Object.assign(node.style, v)
    else if (k === "text") node.textContent = v
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v)
    else if (v !== undefined) node.setAttribute(k, v)
  }
  for (const c of children) if (c) node.appendChild(c)
  return node
}

function ensureStyle() {
  if (document.getElementById("lgav-trim-style")) return
  document.head.appendChild(el("style", { id: "lgav-trim-style", text: STYLE }))
}

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v))
}

function fmtTime(seconds) {
  if (!isFinite(seconds)) return "0:00.00"
  const m = Math.floor(seconds / 60)
  const s = seconds - m * 60
  return `${m}:${s.toFixed(2).padStart(5, "0")}`
}

function splitName(name) {
  const i = name.lastIndexOf("/")
  return i < 0 ? ["", name] : [name.slice(0, i), name.slice(i + 1)]
}

class TrimPanel {
  constructor(node) {
    this.node = node
    this.widgets = {}
    for (const name of ["file", "start_frame", "end_frame", "crop_x", "crop_y", "crop_w", "crop_h"]) {
      this.widgets[name] = node.widgets?.find((w) => w.name === name)
    }
    this.info = null
    this.frame = 0
    this.playing = false
    this.imageMode = false
    this.cropOn = false
    this.aspect = 0
    this.results = null
    this.query = null
    this.selected = null
    this.matchFrames = 1
    this.similarity = 98
    this.busy = false
    this.configuring = false
    this.loadedFile = null
    this.raf = null
    this.saveTimer = null
    this.thumbTimer = null
    this.abort = null

    this.build()
    this.hookWidgets()
    this.refreshAll()
  }

  // ---- widgets --------------------------------------------------------------

  wv(name) {
    const w = this.widgets[name]
    const v = w ? Number(w.value) : 0
    return isFinite(v) ? v : 0
  }

  setW(name, value) {
    const w = this.widgets[name]
    if (!w) return
    if (w.value === value) return
    w.value = value
    this.node.graph?.setDirtyCanvas(true, true)
  }

  get file() {
    const w = this.widgets.file
    return w && typeof w.value === "string" ? w.value : ""
  }

  get frames() {
    return this.info?.frames ?? 0
  }

  get fps() {
    return this.info?.fps > 0 ? this.info.fps : 1
  }

  // The effective range: `end_frame` is exclusive and 0 means the end of the file.
  get start() {
    return clamp(Math.round(this.wv("start_frame")), 0, Math.max(0, this.frames - 1))
  }

  get end() {
    const n = this.frames
    const e = Math.round(this.wv("end_frame"))
    return e <= 0 || e > n ? n : e
  }

  hookWidgets() {
    const hook = (name, fn) => {
      const w = this.widgets[name]
      if (!w) return
      const orig = w.callback
      w.callback = (...args) => {
        const r = orig?.apply(w, args)
        fn()
        return r
      }
    }
    hook("file", () => this.onFileChanged(!this.configuring))
    for (const name of ["start_frame", "end_frame"]) hook(name, () => this.onRangeChanged())
    for (const name of ["crop_x", "crop_y", "crop_w", "crop_h"]) hook(name, () => this.refreshCrop())
  }

  // ---- persistence ---------------------------------------------------------

  // Scrubbing calls this per frame, so the write is debounced; anything that
  // changes the search state writes at once with `saveNow`.
  save() {
    clearTimeout(this.saveTimer)
    this.saveTimer = setTimeout(() => this.saveNow(), 150)
  }

  saveNow() {
    clearTimeout(this.saveTimer)
    this.node.properties ??= {}
    this.node.properties[PROP] = {
      frame: this.frame,
      query: this.query,
      results: this.results,
      matchFrames: this.matchFrames,
      similarity: this.similarity,
      cropOn: this.cropOn,
      aspect: this.aspect,
    }
  }

  restore() {
    const p = this.node.properties?.[PROP]
    if (!p || typeof p !== "object") return
    this.frame = Number(p.frame) || 0
    this.query = p.query == null ? null : Number(p.query)
    this.results = p.results && Array.isArray(p.results.matches) ? p.results : null
    this.matchFrames = clamp(Math.round(Number(p.matchFrames) || 1), 1, 16)
    this.similarity = clamp(Number(p.similarity) || 98, 0, 100)
    this.cropOn = !!p.cropOn
    this.aspect = Number(p.aspect) || 0
    this.ui.matchFrames.value = this.matchFrames
    this.ui.similarity.value = this.similarity
    this.ui.aspect.value = String(this.aspect)
  }

  // ---- DOM -----------------------------------------------------------------

  build() {
    ensureStyle()
    const ui = (this.ui = {})
    const root = (this.root = el("div", { class: "lgav-root", tabindex: "0" }))
    root.addEventListener("keydown", (e) => this.onKey(e))
    root.addEventListener("pointerdown", () => root.focus({ preventScroll: true }))
    // Wheel events are claimed in the capture phase on the document: both node
    // UIs wrap a DOM widget in a container that captures `wheel` and forwards it
    // to the canvas as a zoom before the widget's own listeners ever run. The
    // document's capture listener fires before that container's, so the track
    // can scrub and the results list can scroll; everything else is left to
    // the canvas, so the graph still zooms over the rest of the panel.
    this.onWheelCapture = (e) => {
      if (ui.track.contains(e.target)) {
        if (!this.info || !e.deltaY) return
        e.preventDefault()
        e.stopPropagation()
        if (this.playing) this.pause()
        this.seek(this.frame + Math.sign(e.deltaY) * (e.shiftKey ? 10 : 1))
      } else if (ui.results.contains(e.target) && ui.results.scrollHeight > ui.results.clientHeight) {
        e.stopPropagation()
      }
    }
    document.addEventListener("wheel", this.onWheelCapture, { capture: true, passive: false })

    // Toolbar
    ui.fileInput = el("input", {
      type: "file",
      accept: "video/*,.mkv,.avi,.mov,.m4v,.webm,.mp4,.wmv,.flv,.mpg,.mpeg,.ts,.m2ts",
      style: { display: "none" },
      onchange: (e) => {
        const f = e.target.files?.[0]
        if (f) this.upload(f)
        e.target.value = ""
      },
    })
    ui.upload = el("button", { text: "Upload", title: "Upload a video to the input directory", onclick: () => ui.fileInput.click() })
    ui.info = el("span", { class: "lgav-grow lgav-muted", text: "No file" })
    ui.cropBtn = el("button", { text: "Crop", title: "Show the crop box on the preview", onclick: () => this.toggleCrop() })
    ui.aspect = el("select", { title: "Lock the crop box to an aspect ratio" })
    for (const [label, v] of ASPECTS) ui.aspect.appendChild(el("option", { value: String(v), text: label }))
    ui.aspect.addEventListener("change", () => {
      this.aspect = Number(ui.aspect.value)
      this.applyAspect()
      this.save()
    })
    ui.cropW = el("input", { class: "lgav-small", type: "text", title: "Crop width in pixels" })
    ui.cropH = el("input", { class: "lgav-small", type: "text", title: "Crop height in pixels" })
    const onSize = (isW) => () => this.setCropSize(isW)
    for (const [inp, isW] of [[ui.cropW, true], [ui.cropH, false]]) {
      inp.addEventListener("change", onSize(isW))
      inp.addEventListener("keydown", (e) => {
        e.stopPropagation()
        if (e.key === "Enter") onSize(isW)()
      })
    }
    ui.cropClear = el("button", { text: "Clear", title: "Remove the crop", onclick: () => this.clearCrop() })
    ui.cropTools = el("div", { class: "lgav-row", style: { display: "none", gap: "3px" } }, [
      ui.aspect,
      ui.cropW,
      el("span", { class: "lgav-muted", text: "×" }),
      ui.cropH,
      ui.cropClear,
    ])
    root.appendChild(el("div", { class: "lgav-row" }, [ui.upload, ui.fileInput, ui.info, ui.cropTools, ui.cropBtn]))

    // Preview
    ui.video = el("video", { preload: "metadata", playsinline: "", muted: "" })
    ui.video.muted = true
    ui.video.loop = false
    ui.video.addEventListener("error", () => this.onVideoError())
    ui.video.addEventListener("loadedmetadata", () => this.refreshCrop())
    ui.still = el("img", { class: "lgav-still", style: { display: "none" }, draggable: "false" })
    ui.empty = el("div", { class: "lgav-empty", text: "Choose or drop a video file" })
    ui.drop = el("div", { class: "lgav-drop", text: "Drop to upload" })
    ui.crop = this.buildCropBox()
    ui.preview = el("div", { class: "lgav-preview" }, [ui.video, ui.still, ui.empty, ui.crop, ui.drop])
    this.bindDrop(ui.preview)
    root.appendChild(ui.preview)
    this.resizeObserver = new ResizeObserver(() => this.refreshCrop())
    this.resizeObserver.observe(ui.preview)

    // Transport
    ui.play = el("button", { text: "▶", title: "Play / pause the range (Space)", onclick: () => this.togglePlay() })
    ui.frameInput = el("input", { type: "number", min: "0", step: "1", title: "Playhead frame" })
    ui.frameInput.addEventListener("change", () => this.seek(Number(ui.frameInput.value) || 0))
    ui.frameInput.addEventListener("keydown", (e) => e.stopPropagation())
    ui.time = el("span", { class: "lgav-muted", text: "" })
    ui.setStart = el("button", { text: "Set start", title: "start_frame = playhead ([)", onclick: () => this.setStart(this.frame) })
    ui.setEnd = el("button", { text: "Set end", title: "end_frame = playhead (]); the range stops before this frame", onclick: () => this.setEnd(this.frame) })
    const step = (d, title) => el("button", { text: d > 0 ? `+${d}` : `${d}`, title, onclick: () => this.seek(this.frame + d) })
    root.appendChild(
      el("div", { class: "lgav-row" }, [
        el("button", { text: "|◀", title: "Go to start_frame (Home)", onclick: () => this.seek(this.start) }),
        step(-10, "Back 10 frames (Shift+←)"),
        step(-1, "Back one frame (←)"),
        ui.play,
        step(1, "Forward one frame (→)"),
        step(10, "Forward 10 frames (Shift+→)"),
        el("button", { text: "▶|", title: "Go to end_frame (End)", onclick: () => this.seek(this.end - 1) }),
        ui.frameInput,
        ui.time,
        el("span", { class: "lgav-grow" }),
        ui.setStart,
        ui.setEnd,
      ])
    )

    // Timeline
    ui.ruler = el("div", { class: "lgav-ruler" })
    ui.markers = el("div")
    ui.fill = el("div", { class: "lgav-fill" })
    ui.hStart = el("div", { class: "lgav-rhandle" })
    ui.hEnd = el("div", { class: "lgav-rhandle" })
    ui.playhead = el("div", { class: "lgav-playhead" })
    ui.track = el("div", { class: "lgav-track", title: "Click or drag to scrub; mouse wheel steps one frame (Shift: ten)" }, [ui.fill, ui.markers, ui.hStart, ui.hEnd, ui.playhead])
    this.bindTrack(ui.track)
    ui.rangeLabel = el("div", { class: "lgav-row lgav-muted", style: { minHeight: "16px" } })
    root.appendChild(el("div", { class: "lgav-timeline" }, [ui.ruler, ui.track, ui.rangeLabel]))

    // Loop assist
    ui.matchFrames = el("input", {
      type: "number",
      min: "1",
      max: "16",
      step: "1",
      value: "1",
      title: "Consecutive frames that must all match (1 = the frame alone). 2–3 also checks that the motion continues the same way, but real footage rarely matches that long.",
    })
    ui.similarity = el("input", {
      type: "number",
      min: "0",
      max: "100",
      step: "0.1",
      value: "98",
      title: "Similarity threshold in percent. 100 is identical; a re-encode of the same frame scores about 99.5, neighbouring frames of slow motion about 98–99.",
    })
    for (const inp of [ui.matchFrames, ui.similarity]) {
      inp.addEventListener("keydown", (e) => e.stopPropagation())
      inp.addEventListener("change", () => {
        this.matchFrames = clamp(Math.round(Number(ui.matchFrames.value) || 1), 1, 16)
        this.similarity = clamp(Number(ui.similarity.value) || 0, 0, 100)
        ui.matchFrames.value = this.matchFrames
        ui.similarity.value = this.similarity
        this.saveNow()
      })
    }
    ui.search = el("button", { text: "Find similar", title: "List frames that look like the frame under the playhead", onclick: () => this.search() })
    ui.status = el("span", { class: "lgav-grow lgav-muted", text: "" })
    root.appendChild(
      el("div", { class: "lgav-row" }, [
        el("span", { class: "lgav-accent", text: "Loop assist" }),
        el("span", { class: "lgav-muted", text: "frames" }),
        ui.matchFrames,
        el("span", { class: "lgav-muted", text: "similarity %" }),
        ui.similarity,
        ui.search,
        ui.status,
      ])
    )
    ui.results = el("div", { class: "lgav-results" })
    root.appendChild(ui.results)

    // Thumbnails
    ui.thumbStart = this.buildThumb("Start")
    ui.thumbEnd = this.buildThumb("End")
    root.appendChild(el("div", { class: "lgav-thumbs" }, [ui.thumbStart.box, ui.thumbEnd.box]))
  }

  buildThumb(label) {
    const img = el("img", { draggable: "false" })
    const cap = el("div", { class: "lgav-cap", text: label })
    const box = el("div", { class: "lgav-thumb" }, [el("div", { class: "lgav-img" }, [img]), cap])
    return { box, img, cap }
  }

  buildCropBox() {
    const box = el("div", { class: "lgav-crop", style: { display: "none" } })
    for (const p of [33.33, 66.67]) {
      box.appendChild(el("div", { class: "lgav-grid", style: { left: `${p}%`, top: 0, bottom: 0, borderLeftWidth: "1px" } }))
      box.appendChild(el("div", { class: "lgav-grid", style: { top: `${p}%`, left: 0, right: 0, borderTopWidth: "1px" } }))
    }
    const handles = {
      tl: [{ top: "-2px", left: "-2px" }, { borderTopWidth: "4px", borderLeftWidth: "4px" }, "nwse-resize"],
      tr: [{ top: "-2px", right: "-2px" }, { borderTopWidth: "4px", borderRightWidth: "4px" }, "nesw-resize"],
      bl: [{ bottom: "-2px", left: "-2px" }, { borderBottomWidth: "4px", borderLeftWidth: "4px" }, "nesw-resize"],
      br: [{ bottom: "-2px", right: "-2px" }, { borderBottomWidth: "4px", borderRightWidth: "4px" }, "nwse-resize"],
      t: [{ top: "-2px", left: "50%", marginLeft: "-7px" }, { borderTopWidth: "4px" }, "ns-resize"],
      b: [{ bottom: "-2px", left: "50%", marginLeft: "-7px" }, { borderBottomWidth: "4px" }, "ns-resize"],
      l: [{ left: "-2px", top: "50%", marginTop: "-7px" }, { borderLeftWidth: "4px" }, "ew-resize"],
      r: [{ right: "-2px", top: "50%", marginTop: "-7px" }, { borderRightWidth: "4px" }, "ew-resize"],
    }
    for (const [key, [pos, border, cursor]] of Object.entries(handles)) {
      const h = el("div", { class: "lgav-handle", style: { ...pos, ...border, cursor } })
      h.addEventListener("pointerdown", (e) => this.cropDown(e, key))
      box.appendChild(h)
    }
    box.addEventListener("pointerdown", (e) => {
      if (e.target === box) this.cropDown(e, "move")
    })
    return box
  }

  bindDrop(target) {
    let depth = 0
    const isVideo = (f) => f && (f.type.startsWith("video/") || /\.(mp4|webm|mkv|avi|mov|m4v|wmv|flv|mpg|mpeg|ts|m2ts)$/i.test(f.name))
    target.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer?.types?.includes("Files")) return
      e.preventDefault()
      e.stopPropagation()
      if (++depth === 1) this.ui.drop.style.display = "flex"
    })
    target.addEventListener("dragover", (e) => {
      if (!e.dataTransfer?.types?.includes("Files")) return
      e.preventDefault()
      e.stopPropagation()
    })
    target.addEventListener("dragleave", (e) => {
      e.stopPropagation()
      if (--depth <= 0) {
        depth = 0
        this.ui.drop.style.display = "none"
      }
    })
    target.addEventListener("drop", (e) => {
      depth = 0
      this.ui.drop.style.display = "none"
      const f = e.dataTransfer?.files?.[0]
      if (!isVideo(f)) return
      e.preventDefault()
      e.stopPropagation()
      this.upload(f)
    })
  }

  // ---- file ----------------------------------------------------------------

  async upload(file) {
    const ui = this.ui
    ui.upload.disabled = true
    ui.upload.textContent = "Uploading…"
    try {
      const body = new FormData()
      body.append("image", file)
      const resp = await api.fetchApi("/upload/image", { method: "POST", body })
      if (resp.status !== 200) throw new Error(`${resp.status} ${resp.statusText}`)
      const data = await resp.json()
      const name = data.subfolder ? `${data.subfolder}/${data.name}` : data.name
      const w = this.widgets.file
      if (w) {
        const values = w.options?.values
        if (Array.isArray(values) && !values.includes(name)) values.push(name)
        w.value = name
        w.callback?.(name)
        this.node.graph?.setDirtyCanvas(true, true)
      }
    } catch (err) {
      console.error("[LoadVideoTrim] upload failed", err)
      this.setStatus(`Upload failed: ${err.message || err}`, true)
    } finally {
      ui.upload.disabled = false
      ui.upload.textContent = "Upload"
    }
  }

  onFileChanged(reset) {
    const name = this.file
    if (reset) {
      this.pause()
      for (const k of ["start_frame", "end_frame", "crop_x", "crop_y", "crop_w", "crop_h"]) this.setW(k, 0)
      this.frame = 0
      this.query = null
      this.results = null
      this.selected = null
      this.saveNow()
    }
    if (name === this.loadedFile && this.info) {
      this.refreshAll()
      return
    }
    this.loadedFile = name
    this.info = null
    this.imageMode = false
    this.abort?.abort()
    this.abort = null
    const ui = this.ui
    ui.still.style.display = "none"
    ui.video.style.display = ""
    ui.video.removeAttribute("src")
    ui.video.load()
    this.renderResults()
    if (!name) {
      ui.empty.style.display = "flex"
      ui.info.textContent = "No file"
      this.refreshAll()
      return
    }
    ui.empty.style.display = "none"
    ui.video.src = this.viewURL(name)
    ui.info.textContent = `${name} — indexing…`
    this.fetchInfo(name)
  }

  async fetchInfo(name) {
    const ctrl = (this.abort = new AbortController())
    try {
      const resp = await api.fetchApi(`${ROUTE}/info?file=${encodeURIComponent(name)}`, { signal: ctrl.signal })
      const data = await resp.json()
      if (ctrl.signal.aborted || name !== this.loadedFile) return
      if (!resp.ok) throw new Error(data.error || resp.statusText)
      this.info = data
      this.frame = clamp(this.frame, 0, data.frames - 1)
      const [, base] = splitName(name)
      const seconds = data.frames / (data.fps || 1)
      this.ui.info.textContent = `${base} — ${data.width}×${data.height}, ${data.frames} frames, ${(+data.fps.toFixed(3))} fps, ${seconds.toFixed(2)} s`
      this.ui.info.title = name
      this.refreshAll()
      this.seek(this.frame, true)
    } catch (err) {
      if (ctrl.signal.aborted) return
      console.error("[LoadVideoTrim] info failed", err)
      this.ui.info.textContent = `${name} — ${err.message || err}`
      this.ui.info.classList.add("lgav-error")
      setTimeout(() => this.ui.info.classList.remove("lgav-error"), 4000)
    }
  }

  onVideoError() {
    // The browser cannot play this container; fall back to server-rendered frames.
    if (!this.file) return
    this.imageMode = true
    this.pause()
    this.ui.video.style.display = "none"
    this.ui.still.style.display = ""
    this.seek(this.frame, true)
    this.refreshTransport()
  }

  viewURL(name) {
    const [subfolder, base] = splitName(name)
    return api.apiURL(`/view?filename=${encodeURIComponent(base)}&type=input&subfolder=${encodeURIComponent(subfolder)}`)
  }

  frameURL(frame, size, crop = null) {
    const v = this.info?.key ? `&v=${encodeURIComponent(this.info.key)}` : ""
    const c = crop && crop.active ? `&crop=${crop.x},${crop.y},${crop.w},${crop.h}` : ""
    return api.apiURL(`${ROUTE}/frame?file=${encodeURIComponent(this.file)}&frame=${frame}&size=${size}${v}${c}`)
  }

  // ---- playhead ------------------------------------------------------------

  seek(frame, force = false) {
    if (!this.info) return
    frame = clamp(Math.round(frame), 0, this.frames - 1)
    const changed = frame !== this.frame
    this.frame = frame
    if (this.imageMode) {
      if (changed || force) this.ui.still.src = this.frameURL(frame, PREVIEW_SIZE)
    } else if (changed || force) {
      // Mid-frame time: browsers show the frame whose interval contains currentTime.
      this.ui.video.currentTime = (frame + 0.5) / this.fps
    }
    this.refreshPlayhead()
    this.save()
  }

  togglePlay() {
    if (this.playing) this.pause()
    else this.play()
  }

  play() {
    if (!this.info || this.imageMode) return
    const v = this.ui.video
    if (this.frame < this.start || this.frame >= this.end) this.seek(this.start)
    this.playing = true
    v.play().catch(() => {
      this.playing = false
      this.refreshTransport()
    })
    const tick = () => {
      if (!this.playing) return
      const f = Math.floor(v.currentTime * this.fps)
      if (f >= this.end || f < this.start || v.ended) {
        v.currentTime = (this.start + 0.5) / this.fps
        if (v.ended) v.play().catch(() => {})
        this.frame = this.start
      } else {
        this.frame = f
      }
      this.refreshPlayhead()
      this.raf = requestAnimationFrame(tick)
    }
    this.raf = requestAnimationFrame(tick)
    this.refreshTransport()
  }

  pause() {
    if (this.raf) cancelAnimationFrame(this.raf)
    this.raf = null
    if (!this.playing) return
    this.playing = false
    this.ui.video.pause()
    this.seek(this.frame, true)
    this.refreshTransport()
    this.save()
  }

  onKey(e) {
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return
    const big = e.shiftKey ? 10 : 1
    const handled = {
      ArrowLeft: () => this.seek(this.frame - big),
      ArrowRight: () => this.seek(this.frame + big),
      Home: () => this.seek(this.start),
      End: () => this.seek(this.end - 1),
      " ": () => this.togglePlay(),
      "[": () => this.setStart(this.frame),
      "]": () => this.setEnd(this.frame),
    }[e.key]
    if (!handled) return
    e.preventDefault()
    e.stopPropagation()
    if (this.playing && e.key !== " ") this.pause()
    handled()
  }

  // ---- range ---------------------------------------------------------------

  setStart(frame) {
    if (!this.info) return
    frame = clamp(Math.round(frame), 0, this.frames - 1)
    this.setW("start_frame", frame)
    if (this.end <= frame) this.setW("end_frame", 0)
    this.onRangeChanged()
  }

  setEnd(frame) {
    if (!this.info) return
    frame = clamp(Math.round(frame), 1, this.frames)
    this.setW("end_frame", frame >= this.frames ? 0 : frame)
    if (this.start >= frame) this.setW("start_frame", 0)
    this.onRangeChanged()
  }

  onRangeChanged() {
    this.refreshRange()
    clearTimeout(this.thumbTimer)
    this.thumbTimer = setTimeout(() => this.refreshThumbs(), 120)
  }

  bindTrack(track) {
    let drag = null
    const frameAt = (e) => {
      const r = track.getBoundingClientRect()
      const x = clamp(e.clientX - r.left, 0, r.width)
      return Math.round((x / r.width) * Math.max(1, this.frames - 1))
    }
    const px = (frame) => {
      const r = track.getBoundingClientRect()
      return (frame / Math.max(1, this.frames - 1)) * r.width
    }
    track.addEventListener("pointerdown", (e) => {
      if (!this.info || e.button !== 0) return
      e.preventDefault()
      e.stopPropagation()
      const x = e.clientX - track.getBoundingClientRect().left
      const dStart = Math.abs(x - px(this.start))
      const dEnd = Math.abs(x - px(this.end))
      if (dStart <= HANDLE_GRAB && dStart <= dEnd) drag = "start"
      else if (dEnd <= HANDLE_GRAB) drag = "end"
      else drag = "play"
      if (this.playing) this.pause()
      try {
        track.setPointerCapture(e.pointerId)
      } catch {}
      apply(e)
    })
    const apply = (e) => {
      const f = frameAt(e)
      if (drag === "start") {
        this.setW("start_frame", Math.min(f, this.end - 1))
        this.refreshRange()
        this.seek(this.start)
      } else if (drag === "end") {
        const end = Math.max(f, this.start + 1)
        this.setW("end_frame", end >= this.frames ? 0 : end)
        this.refreshRange()
        this.seek(this.end - 1)
      } else {
        this.seek(f)
      }
    }
    track.addEventListener("pointermove", (e) => {
      if (drag) apply(e)
    })
    const up = (e) => {
      if (!drag) return
      if (drag !== "play") this.onRangeChanged()
      drag = null
      try {
        track.releasePointerCapture(e.pointerId)
      } catch {}
    }
    track.addEventListener("pointerup", up)
    track.addEventListener("pointercancel", up)
  }

  // ---- crop ----------------------------------------------------------------

  // The crop box as the node sees it: pixels, with 0 width/height meaning "to the edge".
  cropRect() {
    const W = this.info?.width ?? 0
    const H = this.info?.height ?? 0
    let x = clamp(Math.round(this.wv("crop_x")), 0, Math.max(0, W - 1))
    let y = clamp(Math.round(this.wv("crop_y")), 0, Math.max(0, H - 1))
    let w = Math.round(this.wv("crop_w"))
    let h = Math.round(this.wv("crop_h"))
    w = w <= 0 ? W - x : Math.min(w, W - x)
    h = h <= 0 ? H - y : Math.min(h, H - y)
    return { x, y, w, h, W, H, active: x > 0 || y > 0 || w < W || h < H }
  }

  writeCrop(x, y, w, h) {
    const W = this.info?.width ?? 0
    const H = this.info?.height ?? 0
    x = clamp(Math.round(x), 0, Math.max(0, W - 1))
    y = clamp(Math.round(y), 0, Math.max(0, H - 1))
    w = clamp(Math.round(w), 1, W - x)
    h = clamp(Math.round(h), 1, H - y)
    this.setW("crop_x", x)
    this.setW("crop_y", y)
    this.setW("crop_w", x === 0 && w === W ? 0 : w)
    this.setW("crop_h", y === 0 && h === H ? 0 : h)
    this.refreshCrop()
  }

  // Where the picture sits inside the preview box (object-fit: contain).
  mediaRect() {
    const box = this.ui.preview
    const W = this.info?.width || this.ui.video.videoWidth || 0
    const H = this.info?.height || this.ui.video.videoHeight || 0
    const cw = box.clientWidth
    const ch = box.clientHeight
    if (!W || !H || !cw || !ch) return null
    const scale = Math.min(cw / W, ch / H)
    const rw = W * scale
    const rh = H * scale
    return { left: (cw - rw) / 2, top: (ch - rh) / 2, scale, W, H }
  }

  toggleCrop() {
    this.cropOn = !this.cropOn
    if (this.cropOn && this.playing) this.pause()
    this.refreshCrop()
    this.save()
  }

  clearCrop() {
    for (const k of ["crop_x", "crop_y", "crop_w", "crop_h"]) this.setW(k, 0)
    this.refreshCrop()
  }

  aspectRatio() {
    if (this.aspect === -1) return this.info ? this.info.width / this.info.height : 0
    return this.aspect > 0 ? this.aspect : 0
  }

  applyAspect() {
    const ar = this.aspectRatio()
    if (!ar || !this.info) return
    const c = this.cropRect()
    let w = c.w
    let h = Math.round(w / ar)
    if (h > c.H) {
      h = c.H
      w = Math.round(h * ar)
    }
    this.writeCrop(Math.min(c.x, c.W - w), Math.min(c.y, c.H - h), w, h)
  }

  setCropSize(isWidth) {
    if (!this.info) return
    const c = this.cropRect()
    let w = parseInt(this.ui.cropW.value) || c.w
    let h = parseInt(this.ui.cropH.value) || c.h
    const ar = this.aspectRatio()
    if (ar) {
      if (isWidth) h = Math.round(w / ar)
      else w = Math.round(h * ar)
    }
    w = clamp(w, 1, c.W)
    h = clamp(h, 1, c.H)
    this.writeCrop(Math.min(c.x, c.W - w), Math.min(c.y, c.H - h), w, h)
  }

  cropDown(e, mode) {
    if (!this.cropOn || !this.info || e.button !== 0) return
    e.preventDefault()
    e.stopPropagation()
    const m = this.mediaRect()
    if (!m) return
    const c0 = this.cropRect()
    const ar = this.aspectRatio()
    const x0 = e.clientX
    const y0 = e.clientY
    const target = e.target
    try {
      target.setPointerCapture(e.pointerId)
    } catch {}
    const move = (ev) => {
      const dx = (ev.clientX - x0) / m.scale
      const dy = (ev.clientY - y0) / m.scale
      let { x, y, w, h } = c0
      const right = c0.x + c0.w
      const bottom = c0.y + c0.h
      if (mode === "move") {
        x = clamp(c0.x + dx, 0, c0.W - c0.w)
        y = clamp(c0.y + dy, 0, c0.H - c0.h)
      } else {
        if (mode.includes("l")) x = clamp(c0.x + dx, 0, right - 1)
        if (mode.includes("r")) w = clamp(c0.w + dx, 1, c0.W - c0.x)
        if (mode.includes("t")) y = clamp(c0.y + dy, 0, bottom - 1)
        if (mode.includes("b")) h = clamp(c0.h + dy, 1, c0.H - c0.y)
        if (mode.includes("l")) w = right - x
        if (mode.includes("t")) h = bottom - y
        if (ar) {
          // Keep the ratio by deriving the axis that was not dragged (or the
          // shorter one on a corner), then re-anchor the moving edges.
          const horizontal = mode === "l" || mode === "r"
          const vertical = mode === "t" || mode === "b"
          if (horizontal) h = w / ar
          else if (vertical) w = h * ar
          else if (w / ar >= h) w = h * ar
          else h = w / ar
          if (mode.includes("l")) x = right - w
          if (mode.includes("t")) y = bottom - h
          if (horizontal) y = clamp(c0.y + (c0.h - h) / 2, 0, c0.H - h)
          if (vertical) x = clamp(c0.x + (c0.w - w) / 2, 0, c0.W - w)
          // Clamp to the frame without breaking the ratio.
          if (x < 0 || x + w > c0.W || y < 0 || y + h > c0.H) {
            const maxW = Math.min(mode.includes("l") ? right : c0.W - x, (mode.includes("t") ? bottom : c0.H - y) * ar)
            w = Math.max(1, Math.min(w, maxW))
            h = w / ar
            if (mode.includes("l")) x = right - w
            if (mode.includes("t")) y = bottom - h
            x = clamp(x, 0, c0.W - w)
            y = clamp(y, 0, c0.H - h)
          }
        }
      }
      this.writeCrop(x, y, w, h)
    }
    const up = () => {
      target.removeEventListener("pointermove", move)
      target.removeEventListener("pointerup", up)
      target.removeEventListener("pointercancel", up)
      try {
        target.releasePointerCapture(e.pointerId)
      } catch {}
    }
    target.addEventListener("pointermove", move)
    target.addEventListener("pointerup", up)
    target.addEventListener("pointercancel", up)
  }

  refreshCrop() {
    const ui = this.ui
    const c = this.cropRect()
    // The thumbnails show the cropped picture, so a changed box re-renders them
    // (debounced: this runs on every drag step and on every resize).
    if (this.info && this.thumbCropKey !== undefined && this.thumbCropKey !== `${c.x},${c.y},${c.w},${c.h}`) {
      clearTimeout(this.thumbTimer)
      this.thumbTimer = setTimeout(() => this.refreshThumbs(), 150)
    }
    const show = this.cropOn && !!this.info
    ui.cropBtn.classList.toggle("lgav-on", this.cropOn)
    ui.cropBtn.textContent = c.active && !this.cropOn ? `Crop ${c.w}×${c.h}` : "Crop"
    ui.cropTools.style.display = show ? "flex" : "none"
    const m = show ? this.mediaRect() : null
    if (!m) {
      ui.crop.style.display = "none"
      return
    }
    if (document.activeElement !== ui.cropW) ui.cropW.value = c.w
    if (document.activeElement !== ui.cropH) ui.cropH.value = c.h
    Object.assign(ui.crop.style, {
      display: "block",
      left: `${m.left + c.x * m.scale}px`,
      top: `${m.top + c.y * m.scale}px`,
      width: `${c.w * m.scale}px`,
      height: `${c.h * m.scale}px`,
    })
  }

  // ---- loop assist ---------------------------------------------------------

  setStatus(text, error = false) {
    this.ui.status.textContent = text
    this.ui.status.classList.toggle("lgav-error", error)
  }

  async search() {
    if (!this.info || this.busy) return
    if (this.playing) this.pause()
    const name = this.file
    const query = this.frame
    this.busy = true
    this.ui.search.disabled = true
    this.setStatus(`Searching for frames like #${query}…`)
    try {
      const url = `${ROUTE}/search?file=${encodeURIComponent(name)}&frame=${query}&match_frames=${this.matchFrames}&similarity=${this.similarity}`
      const resp = await api.fetchApi(url)
      const data = await resp.json()
      if (!resp.ok) throw new Error(data.error || resp.statusText)
      if (name !== this.file) return
      this.query = query
      this.results = data
      this.selected = null
      this.renderResults()
      this.saveNow()
    } catch (err) {
      console.error("[LoadVideoTrim] search failed", err)
      this.setStatus(`Search failed: ${err.message || err}`, true)
    } finally {
      this.busy = false
      this.ui.search.disabled = false
    }
  }

  renderResults() {
    const ui = this.ui
    ui.results.replaceChildren()
    ui.markers.replaceChildren()
    const r = this.results
    if (!r || !this.info) {
      if (!this.busy) this.setStatus(this.info ? "Scrub to a frame, then Find similar." : "")
      return
    }
    const n = r.matches.length
    const note = r.truncated ? ` — showing the best ${n} of ${r.total}; raise the similarity` : ""
    this.setStatus(`#${r.query}: ${n} match${n === 1 ? "" : "es"} at ≥ ${r.threshold}%${r.match_frames > 1 ? ` over ${r.match_frames} frames` : ""}${note}`)
    const denom = Math.max(1, this.frames - 1)
    ui.markers.appendChild(el("div", { class: "lgav-marker lgav-query", style: { left: `${(r.query / denom) * 100}%` } }))
    // The query frame heads the list so one click brings the playhead back to
    // it; the search is often run before the range has been set from it.
    const addRow = (frame, extra, score, cls) => {
      const row = el("div", { class: "lgav-res" + cls + (this.selected === frame ? " lgav-sel" : "") }, [
        el("span", { class: "lgav-accent", text: `#${frame}` }),
        el("span", { class: "lgav-muted", text: extra }),
        el("span", { class: "lgav-grow lgav-muted", text: fmtTime(frame / this.fps) }),
        el("span", { text: score }),
        el("button", {
          text: "start",
          title: `start_frame = ${frame}`,
          onclick: (e) => {
            e.stopPropagation()
            this.setStart(frame)
          },
        }),
        el("button", {
          text: "end",
          title: `end_frame = ${frame} (the range stops before it, so the loop returns to start_frame here)`,
          onclick: (e) => {
            e.stopPropagation()
            this.setEnd(frame)
          },
        }),
      ])
      row.addEventListener("click", () => {
        this.selected = frame
        for (const other of ui.results.children) other.classList.toggle("lgav-sel", other === row)
        if (this.playing) this.pause()
        this.seek(frame)
      })
      ui.results.appendChild(row)
    }
    const qs = r.query_run_start ?? r.query
    const qe = r.query_run_end ?? r.query
    addRow(r.query, qe > qs ? ` query (${qs}–${qe})` : " query", "", " lgav-qrow")
    for (const m of r.matches) {
      ui.markers.appendChild(el("div", { class: "lgav-marker", style: { left: `${(m.frame / denom) * 100}%` } }))
      const run = m.run_end > m.run_start ? ` (${m.run_start}–${m.run_end})` : ""
      addRow(m.frame, run, `${m.score.toFixed(1)}%`, "")
    }
    if (n === 0) {
      // A threshold low enough that the matches never break is not "no match":
      // the query's own run has swallowed the candidates. Say so.
      const span = qe - qs + 1
      const text =
        span >= this.frames
          ? `Every frame clears ${r.threshold}%, so the whole clip is one run around #${r.query} and there is nothing to separate. Raise the similarity.`
          : span > 1
            ? `No other frame reaches the threshold. Frames ${qs}–${qe} around #${r.query} all match and count as the query itself; if a repeat you expect lies inside that run, raise the similarity.`
            : "No other frame reaches the threshold."
      ui.results.appendChild(el("div", { class: "lgav-muted", style: { padding: "2px 4px", whiteSpace: "normal" }, text }))
    }
  }

  // ---- refresh -------------------------------------------------------------

  refreshAll() {
    this.refreshRange()
    this.refreshPlayhead()
    this.refreshTransport()
    this.refreshCrop()
    this.renderResults()
    this.refreshThumbs()
  }

  refreshTransport() {
    const ui = this.ui
    const ok = !!this.info
    ui.play.textContent = this.playing ? "❚❚" : "▶"
    ui.play.disabled = !ok || this.imageMode
    ui.play.title = this.imageMode ? "This container cannot be played by the browser; step through frames instead" : "Play / pause the range (Space)"
    ui.search.disabled = !ok || this.busy
    ui.setStart.disabled = ui.setEnd.disabled = !ok
    ui.cropBtn.disabled = !ok
  }

  refreshPlayhead() {
    const ui = this.ui
    const denom = Math.max(1, this.frames - 1)
    ui.playhead.style.left = `${(clamp(this.frame, 0, denom) / denom) * 100}%`
    if (document.activeElement !== ui.frameInput) ui.frameInput.value = this.frame
    ui.frameInput.max = String(Math.max(0, this.frames - 1))
    ui.time.textContent = this.info ? fmtTime(this.frame / this.fps) : ""
  }

  refreshRange() {
    const ui = this.ui
    const n = this.frames
    const denom = Math.max(1, n - 1)
    const s = this.start
    const e = this.end
    const pct = (f) => `${(clamp(f, 0, denom) / denom) * 100}%`
    ui.hStart.style.left = pct(s)
    ui.hEnd.style.left = pct(e)
    ui.fill.style.left = pct(s)
    ui.fill.style.width = `${(clamp(e - s, 0, denom) / denom) * 100}%`
    ui.ruler.replaceChildren()
    if (n > 0) {
      const ticks = 5
      for (let i = 0; i <= ticks; i++) {
        const f = Math.round(((n - 1) * i) / ticks)
        ui.ruler.appendChild(el("span", { style: { left: `${(i / ticks) * 100}%` }, text: String(f) }))
      }
    }
    ui.rangeLabel.replaceChildren()
    if (!this.info) return
    const count = Math.max(0, e - s)
    const endText = e >= n ? `end of file (#${n})` : `end #${e} (excluded)`
    ui.rangeLabel.appendChild(el("span", { text: `start #${s} → ${endText}: ${count} frames, ${fmtTime(count / this.fps)}` }))
  }

  refreshThumbs() {
    const ui = this.ui
    if (!this.info) {
      this.thumbCropKey = undefined
      for (const t of [ui.thumbStart, ui.thumbEnd]) {
        t.img.removeAttribute("src")
        t.cap.textContent = t === ui.thumbStart ? "Start" : "End"
      }
      return
    }
    const s = this.start
    const e = this.end
    const crop = this.cropRect()
    this.thumbCropKey = `${crop.x},${crop.y},${crop.w},${crop.h}`
    ui.thumbStart.img.src = this.frameURL(s, THUMB_SIZE, crop)
    ui.thumbStart.cap.textContent = `Start #${s}`
    if (e >= this.frames) {
      ui.thumbEnd.img.src = this.frameURL(this.frames - 1, THUMB_SIZE, crop)
      ui.thumbEnd.cap.textContent = `Last #${this.frames - 1} (end of file)`
      ui.thumbEnd.cap.title = "The range runs to the end of the file; this is its last frame."
    } else {
      ui.thumbEnd.img.src = this.frameURL(e, THUMB_SIZE, crop)
      ui.thumbEnd.cap.textContent = `End #${e} (excluded, loop returns here)`
      ui.thumbEnd.cap.title = "This frame is not part of the range. When the range loops, it jumps from the frame before this back to Start, so for a seamless loop this should look like Start."
    }
  }

  // ---- lifecycle -----------------------------------------------------------

  onConfigure() {
    // Widget values and properties have been restored; sync the panel to them.
    this.configuring = true
    try {
      this.restore()
      this.onFileChanged(false)
    } finally {
      this.configuring = false
    }
  }

  destroy() {
    this.pause()
    document.removeEventListener("wheel", this.onWheelCapture, { capture: true })
    this.abort?.abort()
    clearTimeout(this.saveTimer)
    clearTimeout(this.thumbTimer)
    this.resizeObserver?.disconnect()
    this.ui.video.removeAttribute("src")
    this.ui.video.load()
  }
}

app.registerExtension({
  name: "LoadGifAsVideo.LoadVideoTrim.panel",
  nodeCreated(node) {
    if (node.comfyClass !== NODE_CLASS) return
    const panel = new TrimPanel(node)
    node.lgavPanel = panel

    const widget = node.addDOMWidget("trim_panel", "LGAV_TRIM", panel.root, {
      hideOnZoom: false,
      getMinHeight: () => PANEL_MIN_HEIGHT,
      getValue: () => "",
      setValue: () => {},
    })
    widget.serialize = false

    const onConfigure = node.onConfigure
    node.onConfigure = function (info) {
      const r = onConfigure?.apply(this, arguments)
      panel.onConfigure(info)
      return r
    }
    const onRemoved = node.onRemoved
    node.onRemoved = function () {
      panel.destroy()
      return onRemoved?.apply(this, arguments)
    }

    requestAnimationFrame(() => {
      const [w, h] = node.computeSize()
      node.setSize([Math.max(node.size[0], MIN_WIDTH, w), Math.max(node.size[1], h)])
      node.graph?.setDirtyCanvas(true, true)
    })
  },
})
