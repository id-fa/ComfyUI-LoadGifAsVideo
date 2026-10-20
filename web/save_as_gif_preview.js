import { app } from "../../scripts/app.js"

// Claim the image preview for SaveAsGif and SaveAsAnimatedWebP.
//
// Both nodes write a file the frontend can preview perfectly well as an image —
// an <img> animates a GIF or a WebP by itself. But both take a VIDEO input, and
// both node UIs treat "has a VIDEO input slot" as "preview this node as a video":
//
//   // renderer/extensions/vueNodes/components/LGraphNode.vue  (Nodes 2.0)
//   const type =
//     isVideoOutput(newOutputs) ||
//     node.previewMediaType === 'video' ||
//     (!node.previewMediaType && hasVideoInput.value)
//       ? 'video' : 'image'
//
//   const hasVideoInput = computed(() =>
//     lgraphNode.value?.inputs?.some((input) => input.type === 'VIDEO') ?? false)
//
// `hasVideoInput` counts the *slot*, not a link, so an unconnected VIDEO input is
// enough. The video player is then handed the file path, `new URL()` throws, and the
// node shows "Video failed to load / Invalid URL / Error loading video".
//
// Setting `previewMediaType` up front fails the `!node.previewMediaType` guard and
// leaves the result on the image path. The same property short-circuits the older
// LiteGraph UI's `isVideoNode()`, so one line covers both.
//
// The alternative was dropping the VIDEO input, which would cost the direct
// `Load GIF as Video` / `Loop Video` -> `Save as GIF` wiring this package exists
// for. This is the cheaper trade.
const IMAGE_PREVIEW_NODES = new Set(["SaveAsGif", "SaveAsAnimatedWebP"])

app.registerExtension({
  name: "LoadGifAsVideo.SaveAsGif.imagePreview",
  nodeCreated(node) {
    if (IMAGE_PREVIEW_NODES.has(node.comfyClass)) {
      node.previewMediaType = "image"
    }
  },
})
