import { app } from "../../scripts/app.js"

// The size row on SaveAsGif and SaveAsAnimatedWebP: an aspect-ratio picker and
// four scale buttons, sitting right under `width` / `height`.
//
// Both controls only rewrite the two widgets — there is nothing behind them on
// the server, the row is not serialized and never reaches the prompt. That is
// the whole design: pressing "×2" and typing the doubled numbers are the same
// operation, so the node works identically from the API with no row at all.
//
//   aspect   keeps the longer of width/height and recomputes the other so the
//            box has the chosen ratio. It changes the *box* (the canvas `pad`
//            and `fit` work from); the picture inside is never stretched.
//   ⅓ ½ ×2 ×3  multiply both numbers.
//
// A widget is left alone when it is 0 ("derive this axis" — there is no number
// to work from) or when it is fed by a link (the value on screen is not the one
// that will be used). Scaling goes axis by axis, so `512 x 0` doubles to
// `1024 x 0`; the aspect picker needs both numbers and is disabled otherwise.
//
// Values are written as they come out, not snapped to the widgets' step of 8:
// the step is a convenience for the arrows, the server accepts any integer, and
// snapping would turn 16:9 at 500 into 500 x 280.

const SIZE_TOOL_NODES = new Set(["SaveAsGif", "SaveAsAnimatedWebP"])

const ROW_HEIGHT = 30

// Landscape first, then the same ratios turned. `1:1` belongs to neither.
const ASPECTS = [
  [1, 1],
  [5, 4],
  [4, 3],
  [3, 2],
  [16, 9],
  [2, 1],
  [21, 9],
  [4, 5],
  [3, 4],
  [2, 3],
  [9, 16],
  [1, 2],
  [9, 21],
]

const SCALES = [
  { label: "⅓", title: "width and height ÷ 3", factor: 1 / 3 },
  { label: "½", title: "width and height ÷ 2", factor: 1 / 2 },
  { label: "×2", title: "width and height × 2", factor: 2 },
  { label: "×3", title: "width and height × 3", factor: 3 },
]

const STYLE = `
.lgav-size { display:flex; align-items:center; gap:4px; height:100%; box-sizing:border-box;
  padding:3px 6px; font:12px/1.3 sans-serif; color:#ddd; user-select:none; }
.lgav-size select, .lgav-size button { background:#2e2e2e; color:#ddd; border:1px solid #444;
  border-radius:4px; font:inherit; height:22px; box-sizing:border-box; cursor:pointer; }
.lgav-size select { flex:1 1 0; min-width:0; padding:0 4px; }
.lgav-size button { flex:0 0 auto; min-width:30px; padding:0 6px; white-space:nowrap; }
.lgav-size select:hover:enabled, .lgav-size button:hover:enabled { background:#3a3a3a; }
.lgav-size select:disabled, .lgav-size button:disabled { opacity:.4; cursor:default; }
`

function ensureStyle() {
  if (document.getElementById("lgav-size-style")) return
  const style = document.createElement("style")
  style.id = "lgav-size-style"
  style.textContent = STYLE
  document.head.appendChild(style)
}

/** The box with ratio `a:b` whose longer side is the longer side of `w` x `h`. */
export function aspectBox(w, h, a, b) {
  const long = Math.max(w, h)
  return a >= b
    ? [long, Math.max(1, Math.round((long * b) / a))]
    : [Math.max(1, Math.round((long * a) / b)), long]
}

class SizeTools {
  constructor(node) {
    this.node = node
    this.widgets = {
      width: node.widgets?.find((w) => w.name === "width"),
      height: node.widgets?.find((w) => w.name === "height"),
    }
    this.build()
    this.hookWidgets()
    this.refresh()
  }

  build() {
    ensureStyle()
    this.root = document.createElement("div")
    this.root.className = "lgav-size"

    this.select = document.createElement("select")
    this.select.title =
      "Set the aspect ratio of width × height, keeping the longer side. " +
      "Only the box changes — the picture is never stretched."
    this.placeholder = new Option("aspect", "")
    this.select.add(this.placeholder)
    for (const [a, b] of ASPECTS) this.select.add(new Option(`${a}:${b}`, `${a}:${b}`))
    this.select.addEventListener("change", () => {
      const [a, b] = this.select.value.split(":").map(Number)
      if (a > 0 && b > 0) this.applyAspect(a, b)
      this.refresh()
    })
    this.root.appendChild(this.select)

    this.buttons = SCALES.map(({ label, title, factor }) => {
      const button = document.createElement("button")
      button.type = "button"
      button.textContent = label
      button.title = title
      button.addEventListener("click", () => {
        this.applyScale(factor)
        this.refresh()
      })
      this.root.appendChild(button)
      return button
    })

    // The widgets can change without a callback (a link made or cut, undo, a
    // script); catching up when the pointer arrives is cheap and always right.
    this.root.addEventListener("pointerenter", () => this.refresh())
  }

  hookWidgets() {
    for (const w of Object.values(this.widgets)) {
      if (!w) continue
      const orig = w.callback
      w.callback = (...args) => {
        const r = orig?.apply(w, args)
        this.refresh()
        return r
      }
    }
  }

  value(name) {
    const v = Math.round(Number(this.widgets[name]?.value))
    return Number.isFinite(v) ? v : 0
  }

  /** Fed by a link: the number on the widget is not what the node will get. */
  linked(name) {
    const w = this.widgets[name]
    if (!w) return true
    if (w.type === "converted-widget") return true
    const slot = this.node.inputs?.find((i) => i.widget?.name === name)
    return slot != null && slot.link != null
  }

  /** A number the row may rewrite: present, not linked, and not the "derive" 0. */
  editable(name) {
    return !this.linked(name) && this.value(name) > 0
  }

  write(name, value) {
    const w = this.widgets[name]
    const max = Number(w.options?.max) || 8192
    const next = Math.min(max, Math.max(1, Math.round(value)))
    if (w.value !== next) w.value = next
  }

  applyAspect(a, b) {
    if (!this.editable("width") || !this.editable("height")) return
    const [w, h] = aspectBox(this.value("width"), this.value("height"), a, b)
    this.write("width", w)
    this.write("height", h)
    this.node.graph?.setDirtyCanvas(true, true)
  }

  applyScale(factor) {
    const names = ["width", "height"].filter((name) => this.editable(name))
    if (!names.length) return
    // Stop at the widgets' limit as a pair: clamping each number on its own
    // would turn 6000 x 3000 into 8192 x 8192 and lose the box's shape.
    const room = Math.min(
      ...names.map((name) => (Number(this.widgets[name].options?.max) || 8192) / this.value(name)),
    )
    const scale = Math.min(factor, room)
    // The 1e-9 keeps a product that is a whole number on paper from depending
    // on how the factor happened to round in binary.
    for (const name of names) this.write(name, this.value(name) * scale + 1e-9)
    this.node.graph?.setDirtyCanvas(true, true)
  }

  refresh() {
    const width = this.editable("width")
    const height = this.editable("height")
    this.select.disabled = !(width && height)
    for (const button of this.buttons) button.disabled = !(width || height)

    // Show the ratio the box already has, when it is one of the presets.
    let current = ""
    if (width && height) {
      const w = this.value("width")
      const h = this.value("height")
      for (const [a, b] of ASPECTS) {
        const [bw, bh] = aspectBox(w, h, a, b)
        if (bw === w && bh === h) {
          current = `${a}:${b}`
          break
        }
      }
    }
    this.select.value = current
  }
}

app.registerExtension({
  name: "LoadGifAsVideo.SaveNodes.sizeTools",
  nodeCreated(node) {
    if (!SIZE_TOOL_NODES.has(node.comfyClass)) return
    const tools = new SizeTools(node)
    if (!tools.widgets.width || !tools.widgets.height) return
    node.lgavSizeTools = tools

    const widget = node.addDOMWidget("size_tools", "LGAV_SIZE_TOOLS", tools.root, {
      hideOnZoom: false,
      serialize: false,
      getMinHeight: () => ROW_HEIGHT,
      getMaxHeight: () => ROW_HEIGHT,
      getValue: () => "",
      setValue: () => {},
    })
    widget.serialize = false

    // Directly under `height`. Safe for saved workflows: a widget with
    // `serialize === false` is skipped when `widgets_values` is written and
    // when it is read back, so the row takes no slot wherever it sits.
    const from = node.widgets.indexOf(widget)
    const after = node.widgets.indexOf(tools.widgets.height)
    if (from > after + 1) {
      node.widgets.splice(from, 1)
      node.widgets.splice(after + 1, 0, widget)
    }

    const onConfigure = node.onConfigure
    node.onConfigure = function () {
      const r = onConfigure?.apply(this, arguments)
      tools.refresh()
      return r
    }
    const onConnectionsChange = node.onConnectionsChange
    node.onConnectionsChange = function () {
      const r = onConnectionsChange?.apply(this, arguments)
      tools.refresh()
      return r
    }

    requestAnimationFrame(() => {
      const [w, h] = node.computeSize()
      node.setSize([Math.max(node.size[0], w), Math.max(node.size[1], h)])
      node.graph?.setDirtyCanvas(true, true)
    })
  },
})
