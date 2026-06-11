import { Application, Ticker } from 'pixi.js'
import { Config, CubismSetting, Live2DSprite } from 'easy-live2d'

Config.MouseFollow = false

interface AssetMap {
  [path: string]: string
}

interface KeyOverlay {
  image: string
  side: 'left' | 'right'
}

interface Live2DLoadPayload {
  renderer: 'live2d'
  modelJson: Record<string, unknown>
  assetBaseUrl?: string
  assetVersion?: string
  assets?: AssetMap
  background?: string
  keys?: Record<string, KeyOverlay>
  transparentParameterIds?: string[]
  hiddenPartIds?: string[]
  hiddenDrawableIds?: string[]
  hiddenDrawableIndices?: number[]
}

interface SpriteState {
  row?: number
  column?: number
  frames?: number
  frameCount?: number
  durationMs?: number
  frameDurationMs?: number
}

interface SpritesheetLoadPayload {
  renderer: 'spritesheet'
  image: string
  atlas: {
    columns: number
    rows: number
    cellWidth: number
    cellHeight: number
  }
  states: Record<string, SpriteState>
}

type LoadPayload = Live2DLoadPayload | SpritesheetLoadPayload

const background = document.getElementById('background') as HTMLImageElement
const live2dCanvas = document.getElementById('live2dCanvas') as HTMLCanvasElement
const spritesheetCanvas = document.getElementById('spritesheetCanvas') as HTMLCanvasElement
const spritesheetContext = spritesheetCanvas.getContext('2d')!
const overlays = document.getElementById('overlays') as HTMLDivElement
const loading = document.getElementById('loading') as HTMLDivElement

let app: Application | null = null
let model: Live2DSprite | null = null
let currentKeys: Record<string, KeyOverlay> = {}
let pressedKeys = new Map<string, HTMLImageElement>()
let keyReleaseTimers = new Map<string, number>()
let spriteImage: HTMLImageElement | null = null
let spriteAtlas: SpritesheetLoadPayload['atlas'] | null = null
let spriteStates: Record<string, SpriteState> = {}
let spriteState = 'idle'
let spriteFrame = 0
let spriteTimer: number | undefined
let latestMouse: { x: number; y: number } | null = null
let smoothedMouse: { x: number; y: number } | null = null
let mouseTickerStarted = false
let hiddenPartIds: string[] = []
let hiddenDrawableIds = new Set<string>()
let hiddenDrawableIndices = new Set<number>()
let activeRenderer: 'live2d' | 'spritesheet' | null = null
let live2dReady = false
let lastError: string | null = null
let live2dModelSize: { width: number; height: number } | null = null

const DAMPING_DECAY = 0.75
const MOUSE_PARAMETER_IDS = [
  'ParamMouseX',
  'ParamMouseY',
  'ParamAngleX',
  'ParamAngleY',
  'ParamAngleZ',
  'ParamEyeBallX',
  'ParamEyeBallY',
]

function setLoading(value: boolean) {
  loading.style.display = value ? 'grid' : 'none'
}

function setRenderer(renderer: 'live2d' | 'spritesheet') {
  activeRenderer = renderer
  live2dCanvas.style.display = renderer === 'live2d' ? 'block' : 'none'
  live2dCanvas.style.opacity = '1'
  spritesheetCanvas.style.display = renderer === 'spritesheet' ? 'block' : 'none'
}

function setBackground(src?: string | null) {
  if (src) {
    background.src = src
    background.style.display = 'block'
  } else {
    background.removeAttribute('src')
    background.style.display = 'none'
  }
}

async function ensureApp() {
  if (app) return app
  app = new Application()
  await app.init({
    view: live2dCanvas,
    resizeTo: window,
    background: 'rgba(0,0,0,0)',
    backgroundColor: 0x000000,
    backgroundAlpha: 0,
    clearBeforeRender: true,
    preference: 'webgl',
    antialias: true,
    autoDensity: true,
    resolution: devicePixelRatio,
    webgl: {
      alpha: true,
      premultipliedAlpha: false,
      preserveDrawingBuffer: true,
    },
  })
  app.start()
  return app
}

function destroyLive2D() {
  live2dReady = false
  live2dModelSize = null
  hiddenPartIds = []
  hiddenDrawableIds = new Set()
  hiddenDrawableIndices = new Set()
  if (!model) return
  Ticker.shared.remove(applyHiddenParts)
  app?.stage.removeChild(model)
  model.destroy()
  model = null
}

function clearSpritesheet() {
  if (spriteTimer) {
    window.clearTimeout(spriteTimer)
    spriteTimer = undefined
  }
  spriteImage = null
  spriteAtlas = null
  spriteStates = {}
  spritesheetContext.clearRect(0, 0, spritesheetCanvas.width, spritesheetCanvas.height)
}

function clearKeyOverlays() {
  for (const timer of keyReleaseTimers.values()) {
    window.clearTimeout(timer)
  }
  keyReleaseTimers.clear()
  pressedKeys.clear()
  overlays.replaceChildren()
  setParameter('CatParamLeftHandDown', false)
  setParameter('CatParamRightHandDown', false)
}

function encodeAssetPath(file: string) {
  return file
    .replaceAll('\\', '/')
    .split('/')
    .filter(Boolean)
    .map((part) => encodeURIComponent(part))
    .join('/')
}

function resolveAsset(payload: Live2DLoadPayload, file: string) {
  const normalized = file.replaceAll('\\', '/')
  const mapped = payload.assets?.[file] ?? payload.assets?.[normalized]
  if (mapped) return mapped
  const base = payload.assetBaseUrl
  if (!base) return file
  const query = payload.assetVersion ? `?v=${encodeURIComponent(payload.assetVersion)}` : ''
  return `${base}/${encodeAssetPath(normalized)}${query}`
}

async function loadLive2D(payload: Live2DLoadPayload) {
  setLoading(true)
  setRenderer('live2d')
  lastError = null
  live2dReady = false
  clearSpritesheet()
  destroyLive2D()
  currentKeys = payload.keys ?? {}
  clearKeyOverlays()
  setBackground(payload.background)

  try {
    const app = await ensureApp()
    app.stage.removeChildren()
    const settings = new CubismSetting({ modelJSON: payload.modelJson })
    settings.redirectPath(({ file }) => resolveAsset(payload, file))
    model = new Live2DSprite({
      modelSetting: settings,
      ticker: Ticker.shared,
    })
    model.renderable = true
    app.stage.addChild(model)
    app.start()
    app.render()
    const bootstrapRender = (model as unknown as { renderFrame?: (renderer: unknown) => Promise<void> }).renderFrame
    if (bootstrapRender) {
      await bootstrapRender.call(model, app.renderer)
    }
    await model.ready
    await nextAnimationFrame()
    live2dModelSize = readNaturalModelSize()
    hiddenPartIds = payload.hiddenPartIds ?? []
    hiddenDrawableIds = new Set(payload.hiddenDrawableIds ?? [])
    hiddenDrawableIndices = new Set(payload.hiddenDrawableIndices ?? [])
    applyTransparentParameters(payload.transparentParameterIds ?? [])
    applyHiddenParts()
    applyDrawableFilter()
    live2dReady = true
    ensureMouseTicker()
    ensureHiddenPartTicker()
    resizeLive2D()
  } catch (error) {
    lastError = error instanceof Error ? error.message : String(error)
    // Keep the window alive so the Python controller can query diagnostics.
    console.error(error)
  } finally {
    setLoading(false)
  }
}

function nextAnimationFrame() {
  return new Promise<void>((resolve) => {
    window.requestAnimationFrame(() => resolve())
  })
}

function readNaturalModelSize() {
  if (!model) return { width: 1, height: 1 }

  const localModel = model as unknown as {
    getLocalModelWidth?: () => number
    getLocalModelHeight?: () => number
    getModelCanvasSize?: () => { width?: number; height?: number } | null
  }
  const canvasSize = localModel.getModelCanvasSize?.()
  const width = localModel.getLocalModelWidth?.() || canvasSize?.width || model.width || 1
  const height = localModel.getLocalModelHeight?.() || canvasSize?.height || model.height || 1

  return {
    width: Number.isFinite(width) && width > 0 ? width : 1,
    height: Number.isFinite(height) && height > 0 ? height : 1,
  }
}

function resizeLive2D() {
  if (!model) return
  const { width, height } = live2dModelSize ?? readNaturalModelSize()
  const scale = Math.min(window.innerWidth / width, window.innerHeight / height)
  model.scale.set(Number.isFinite(scale) && scale > 0 ? scale : 1)
  model.x = window.innerWidth / 2
  model.y = window.innerHeight / 2
  model.anchor.set(0.5)
}

function loadSpritesheet(payload: SpritesheetLoadPayload) {
  setRenderer('spritesheet')
  lastError = null
  destroyLive2D()
  currentKeys = {}
  clearKeyOverlays()
  setBackground(null)
  spriteAtlas = payload.atlas
  spriteStates = payload.states
  spriteState = 'idle'
  spriteFrame = 0
  spriteImage = new Image()
  spriteImage.onload = () => drawSpriteFrame()
  spriteImage.src = payload.image
}

function drawSpriteFrame() {
  if (!spriteImage || !spriteAtlas) return
  const state = spriteStates[spriteState] ?? spriteStates.idle ?? Object.values(spriteStates)[0]
  if (!state) return
  const frames = Math.max(1, state.frames ?? state.frameCount ?? 1)
  const row = state.row ?? 0
  const column = state.column ?? 0
  const sx = (column + (spriteFrame % frames)) * spriteAtlas.cellWidth
  const sy = row * spriteAtlas.cellHeight
  spritesheetCanvas.width = window.innerWidth
  spritesheetCanvas.height = window.innerHeight
  spritesheetContext.clearRect(0, 0, spritesheetCanvas.width, spritesheetCanvas.height)
  spritesheetContext.drawImage(
    spriteImage,
    sx,
    sy,
    spriteAtlas.cellWidth,
    spriteAtlas.cellHeight,
    0,
    0,
    window.innerWidth,
    window.innerHeight,
  )
  spriteFrame += 1
  const duration = Math.max(30, state.durationMs ?? state.frameDurationMs ?? 120)
  spriteTimer = window.setTimeout(drawSpriteFrame, duration)
}

function setParameter(id: string, value: number | boolean) {
  model?.setParameterValueById(id, Number(value))
}

function parameterRange(id: string) {
  return model?.getParameterValueRangeById(id)
}

function applyTransparentParameters(ids: string[]) {
  for (const id of ids) {
    const range = parameterRange(id)
    const value = range?.max ?? 1
    setParameter(id, value)
  }
}

function applyHiddenParts() {
  const cubismModel = (model as unknown as {
    _model?: {
      getModel?: () => {
        getPartCount?: () => number
        getPartId?: (index: number) => { getString?: () => { s?: string } } | string
        setPartOpacityByIndex?: (index: number, opacity: number) => void
      } | null
    }
  } | null)?._model?.getModel?.()
  if (!cubismModel?.getPartCount || !cubismModel.getPartId || !cubismModel.setPartOpacityByIndex) return

  const hidden = new Set(hiddenPartIds)
  const count = cubismModel.getPartCount()
  for (let index = 0; index < count; index += 1) {
    const partHandle = cubismModel.getPartId(index)
    const partId = typeof partHandle === 'string' ? partHandle : partHandle?.getString?.().s
    if (!partId || !hidden.has(partId)) continue
    cubismModel.setPartOpacityByIndex(index, 0)
  }
}

function ensureHiddenPartTicker() {
  Ticker.shared.remove(applyHiddenParts)
  if (hiddenPartIds.length) {
    Ticker.shared.add(applyHiddenParts)
  }
}

function drawableId(coreModel: { getDrawableId?: (index: number) => { getString?: () => { s?: string } } | string }, index: number) {
  const handle = coreModel.getDrawableId?.(index)
  return typeof handle === 'string' ? handle : handle?.getString?.().s
}

function applyDrawableFilter() {
  const live2dModel = (model as unknown as {
    _model?: {
      getRenderer?: () => {
        drawMeshWebGL?: (coreModel: unknown, index: number) => void
        __cbPetOriginalDrawMeshWebGL?: (coreModel: unknown, index: number) => void
      } | null
    }
  } | null)?._model
  const renderer = live2dModel?.getRenderer?.()
  if (!renderer?.drawMeshWebGL) return

  if (!renderer.__cbPetOriginalDrawMeshWebGL) {
    renderer.__cbPetOriginalDrawMeshWebGL = renderer.drawMeshWebGL
  }

  renderer.drawMeshWebGL = function drawMeshWebGL(coreModel: unknown, index: number) {
    const id = drawableId(coreModel as { getDrawableId?: (index: number) => { getString?: () => { s?: string } } | string }, index)
    if (hiddenDrawableIndices.has(index) || (id && hiddenDrawableIds.has(id))) {
      return
    }
    return renderer.__cbPetOriginalDrawMeshWebGL?.call(this, coreModel, index)
  }
}

function setHiddenDrawables(indices: number[] = [], ids: string[] = []) {
  hiddenDrawableIndices = new Set(indices.map((value) => Number(value)).filter(Number.isFinite))
  hiddenDrawableIds = new Set(ids.map((value) => String(value)))
  applyDrawableFilter()
}

function supportedKey(key: string): string | undefined {
  if (currentKeys[key]) return key

  if (/^F\d+$/.test(key) && currentKeys.Fn) {
    return 'Fn'
  }

  for (const base of ['Meta', 'Shift', 'Alt', 'Control']) {
    if (key.startsWith(base) && currentKeys[base]) {
      return base
    }
  }

  return undefined
}

function updateHandParameters() {
  let left = false
  let right = false

  for (const key of pressedKeys.keys()) {
    const overlay = currentKeys[key]
    if (!overlay) continue
    if (overlay.side === 'left') left = true
    if (overlay.side === 'right') right = true
  }

  setParameter('CatParamLeftHandDown', left)
  setParameter('CatParamRightHandDown', right)
}

function releaseKey(key: string) {
  const timer = keyReleaseTimers.get(key)
  if (timer) {
    window.clearTimeout(timer)
    keyReleaseTimers.delete(key)
  }
  pressedKeys.get(key)?.remove()
  pressedKeys.delete(key)
  updateHandParameters()
}

function pressKey(key: string, pressed: boolean, autoReleaseMs = 0) {
  const normalized = supportedKey(key)
  if (!normalized) return

  const overlay = currentKeys[normalized]

  if (!pressed) {
    releaseKey(normalized)
    return
  }

  for (const pressedKey of [...pressedKeys.keys()]) {
    if (pressedKey !== normalized && currentKeys[pressedKey]?.side === overlay.side) {
      releaseKey(pressedKey)
    }
  }

  if (!pressedKeys.has(normalized)) {
    const image = document.createElement('img')
    image.src = overlay.image
    overlays.appendChild(image)
    pressedKeys.set(normalized, image)
  }

  updateHandParameters()

  const previousTimer = keyReleaseTimers.get(normalized)
  if (previousTimer) window.clearTimeout(previousTimer)
  if (autoReleaseMs > 0) {
    keyReleaseTimers.set(normalized, window.setTimeout(() => releaseKey(normalized), autoReleaseMs))
  }
}

function mouseButton(button: string, pressed: boolean) {
  if (button === 'left') setParameter('ParamMouseLeftDown', pressed)
  if (button === 'right') setParameter('ParamMouseRightDown', pressed)
}

function applyMouseMove(xRatio: number, yRatio: number) {
  for (const id of MOUSE_PARAMETER_IDS) {
    const range = parameterRange(id)
    if (!range) continue
    const min = range.min
    const max = range.max
    if (min == null || max == null) continue
    const isX = id.endsWith('X')
    const isZ = id.endsWith('Z')
    let value = 0
    if (isZ) {
      value = (1 - 2 * xRatio) * (1 - 2 * yRatio) * min
    } else {
      const ratio = isX ? xRatio : yRatio
      value = max - ratio * (max - min)
    }
    setParameter(id, value)
  }
}

function handleMouseTicker(ticker: Ticker) {
  const destination = latestMouse
  if (!destination || !model) return

  const current = smoothedMouse ?? destination
  const alpha = 1 - DAMPING_DECAY ** (ticker.deltaMS / (1000 / 60))
  const interpolated = {
    x: current.x + (destination.x - current.x) * alpha,
    y: current.y + (destination.y - current.y) * alpha,
  }

  if (Math.hypot(destination.x - interpolated.x, destination.y - interpolated.y) < 0.001) {
    smoothedMouse = { ...destination }
    latestMouse = null
  } else {
    smoothedMouse = interpolated
  }

  applyMouseMove(smoothedMouse.x, smoothedMouse.y)
}

function ensureMouseTicker() {
  if (mouseTickerStarted) return
  Ticker.shared.add(handleMouseTicker)
  mouseTickerStarted = true
}

function mouseMove(xRatio: number, yRatio: number) {
  latestMouse = {
    x: Math.max(0, Math.min(xRatio, 1)),
    y: Math.max(0, Math.min(yRatio, 1)),
  }
}

function setState(state: string) {
  spriteState = state || 'idle'
  spriteFrame = 0
  if (spriteImage) drawSpriteFrame()
}

function handleWheel(event: WheelEvent) {
  event.preventDefault()
  void window.pywebview?.api?.petWheel?.(event.deltaY)
}

window.addEventListener('resize', () => {
  resizeLive2D()
  if (spriteImage) drawSpriteFrame()
})

window.addEventListener('wheel', handleWheel, { passive: false })

window.cbPet = {
  load(payload: LoadPayload) {
    if (payload.renderer === 'spritesheet') {
      loadSpritesheet(payload)
    } else {
      void loadLive2D(payload)
    }
  },
  pressKey,
  mouseButton,
  mouseMove,
  setState,
  setHiddenDrawables,
  getStatus() {
    const internals = model as unknown as {
      getModelCanvasSize?: () => { width: number; height: number; pixelsPerUnit: number } | null
      getLocalModelWidth?: () => number
      getLocalModelHeight?: () => number
      _model?: {
        isReady?: boolean
        getModel?: () => unknown
      } | null
      _renderInitialized?: boolean
      _renderInitializing?: boolean
      _cubismInitialized?: boolean
      _modelRenderer?: unknown
    } | null
    const canvasSize = internals?.getModelCanvasSize?.()
    const bounds = model?.getBounds?.()
    return {
      renderer: activeRenderer,
      live2dReady,
      modelWidth: model?.width ?? null,
      modelHeight: model?.height ?? null,
      modelNaturalWidth: live2dModelSize?.width ?? null,
      modelNaturalHeight: live2dModelSize?.height ?? null,
      localModelWidth: (model as unknown as { getLocalModelWidth?: () => number } | null)?.getLocalModelWidth?.() ?? null,
      localModelHeight: (model as unknown as { getLocalModelHeight?: () => number } | null)?.getLocalModelHeight?.() ?? null,
      modelCanvasSize: canvasSize ?? null,
      internalModelPresent: Boolean(internals?._model),
      internalModelReady: internals?._model?.isReady ?? null,
      coreModelPresent: Boolean(internals?._model?.getModel?.()),
      renderInitialized: internals?._renderInitialized ?? null,
      renderInitializing: internals?._renderInitializing ?? null,
      cubismInitialized: internals?._cubismInitialized ?? null,
      modelRendererPresent: Boolean(internals?._modelRenderer),
      bounds: bounds
        ? { minX: bounds.minX, minY: bounds.minY, maxX: bounds.maxX, maxY: bounds.maxY, width: bounds.width, height: bounds.height }
        : null,
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      live2dCanvasWidth: live2dCanvas.width,
      live2dCanvasHeight: live2dCanvas.height,
      webglAvailable: Boolean(live2dCanvas.getContext('webgl') || live2dCanvas.getContext('webgl2')),
      keyCount: Object.keys(currentKeys).length,
      error: lastError,
    }
  },
}

declare global {
  interface Window {
    pywebview?: {
      api?: {
        petWheel?: (deltaY: number) => Promise<boolean>
      }
    }
    cbPet: {
      load(payload: LoadPayload): void
      pressKey(key: string, pressed: boolean): void
      pressKey(key: string, pressed: boolean, autoReleaseMs?: number): void
      mouseButton(button: string, pressed: boolean): void
      mouseMove(xRatio: number, yRatio: number): void
      setState(state: string): void
      setHiddenDrawables(indices?: number[], ids?: string[]): void
      getStatus(): {
        renderer: 'live2d' | 'spritesheet' | null
        live2dReady: boolean
        modelWidth: number | null
        modelHeight: number | null
        modelNaturalWidth: number | null
        modelNaturalHeight: number | null
        localModelWidth: number | null
        localModelHeight: number | null
        modelCanvasSize: { width: number; height: number; pixelsPerUnit: number } | null
        internalModelPresent: boolean
        internalModelReady: boolean | null
        coreModelPresent: boolean
        renderInitialized: boolean | null
        renderInitializing: boolean | null
        cubismInitialized: boolean | null
        modelRendererPresent: boolean
        bounds: { minX: number; minY: number; maxX: number; maxY: number; width: number; height: number } | null
        innerWidth: number
        innerHeight: number
        live2dCanvasWidth: number
        live2dCanvasHeight: number
        webglAvailable: boolean
        keyCount: number
        error: string | null
      }
    }
  }
}
