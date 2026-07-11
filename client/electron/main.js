const { app, BrowserWindow, Menu, shell } = require('electron')
const path = require('path')

// 开发模式：连接 Vite dev server
const isDev = process.env.NODE_ENV === 'development'

let mainWindow = null

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    icon: path.join(__dirname, '../web/public/favicon.ico'),
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      devTools: isDev,
      webSecurity: true,
    },
    titleBarStyle: 'hiddenInset',
    backgroundColor: '#0f1419',
  })

  // 隐藏菜单栏
  Menu.setApplicationMenu(null)

  const configuredAppUrl = process.env.TWELVE_C_APP_URL?.trim()
  if (isDev) {
    mainWindow.loadURL('http://localhost:5173')
    mainWindow.webContents.openDevTools()
  } else if (configuredAppUrl) {
    mainWindow.loadURL(configuredAppUrl)
  } else {
    // 生产模式：加载构建后的文件
    const indexPath = path.join(__dirname, '../web/dist/index.html')
    mainWindow.loadFile(indexPath)
  }

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) {
      void shell.openExternal(url)
    }
    return { action: 'deny' }
  })

  mainWindow.webContents.on('will-navigate', (event, url) => {
    const currentUrl = mainWindow?.webContents.getURL() ?? ''
    if (url !== currentUrl) {
      event.preventDefault()
    }
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

app.whenReady().then(createWindow)

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
