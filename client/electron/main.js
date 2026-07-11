const { app, BrowserWindow, Menu, session, shell } = require('electron')
const fs = require('fs')
const path = require('path')

const isDev = process.argv.includes('--dev') || process.env.NODE_ENV === 'development'
const DEV_URL = process.env.TWELVEC_DEV_URL || 'http://127.0.0.1:5173'

let mainWindow = null

function loadProductionUrl() {
  const configPath = path.join(__dirname, 'app.config.json')
  if (!fs.existsSync(configPath)) {
    throw new Error(
      'Missing electron/app.config.json. Run npm run electron:prepare before packaging.',
    )
  }

  const config = JSON.parse(fs.readFileSync(configPath, 'utf8'))
  if (typeof config.appUrl !== 'string' || !config.appUrl.trim()) {
    throw new Error('electron/app.config.json requires a non-empty appUrl')
  }

  const url = new URL(config.appUrl.trim())
  if (url.protocol !== 'https:') {
    throw new Error('Production appUrl must use HTTPS')
  }
  if (url.username || url.password) {
    throw new Error('Production appUrl must not contain credentials')
  }
  url.hash = ''
  return url
}

function targetUrl() {
  return isDev ? new URL(DEV_URL) : loadProductionUrl()
}

function isAllowedNavigation(url, allowedOrigin) {
  try {
    return new URL(url).origin === allowedOrigin
  } catch {
    return false
  }
}

function createWindow() {
  const startUrl = targetUrl()
  const allowedOrigin = startUrl.origin

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    show: false,
    autoHideMenuBar: true,
    backgroundColor: '#ffffff',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      devTools: isDev,
      webSecurity: true,
      allowRunningInsecureContent: false,
      spellcheck: false,
    },
  })

  Menu.setApplicationMenu(null)

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) {
      void shell.openExternal(url)
    }
    return { action: 'deny' }
  })

  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!isAllowedNavigation(url, allowedOrigin)) {
      event.preventDefault()
      if (url.startsWith('https://')) {
        void shell.openExternal(url)
      }
    }
  })

  mainWindow.webContents.on('will-redirect', (event, url) => {
    if (!isAllowedNavigation(url, allowedOrigin)) {
      event.preventDefault()
    }
  })

  mainWindow.once('ready-to-show', () => {
    mainWindow?.show()
  })

  void mainWindow.loadURL(startUrl.toString())

  if (isDev) {
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  }

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

app.whenReady().then(() => {
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false)
  })
  session.defaultSession.setPermissionCheckHandler(() => false)
  createWindow()
})

app.on('web-contents-created', (_event, contents) => {
  contents.on('will-attach-webview', (event) => {
    event.preventDefault()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

app.on('activate', () => {
  if (mainWindow === null) {
    createWindow()
  }
})
