# 12C Client

浏览器端与 TypeScript 传输层（npm workspaces：`transfer` / `app` / `web`），加密核心来自 `core/twelve_c_wasm`（Emscripten）或 `core/12c_file_transfer_scheme`（原生 C++）。

## 构建脚本一览

同步仓库后，**以下脚本应存在于 `client/` 树中**。若缺失，从版本库恢复或对照本表检查路径。

### 推荐入口（`client/` 根目录）

| 脚本 | 平台 | 作用 |
|------|------|------|
| [`build.ps1`](build.ps1) | Windows | 一键：`npm install` → `build-ts` → WASM（若缺）→ `copy:wasm` → 可选 `-Production` |
| [`build.sh`](build.sh) | Unix | 同上 |
| [`build-ts.ps1`](build-ts.ps1) | Windows | 仅编译 `transfer` / `app` TypeScript → `dist/` |
| [`build-ts.sh`](build-ts.sh) | Unix | 同上 |
| [`start.ps1`](start.ps1) | Windows | 构建（如需）+ 启动 Vite dev |
| [`start.sh`](start.sh) | Unix | 同上 |

等效 npm（在 `client/` 目录）：

```bash
npm run build          # → build.ps1（ts + wasm + copy）
npm run build:sh       # → build.sh
npm run build:ts       # → build-ts.ps1（仅 TypeScript）
npm run build:ts:sh    # → build-ts.sh
npm run build:prod     # → build.ps1 -Production
npm run start          # → start.ps1
npm run start:sh       # → start.sh
npm run build:wasm     # → twelve_c_wasm/build-wasm.ps1（仅 WASM）
npm run setup:emsdk    # → twelve_c_wasm/setup-emsdk.ps1
npm run build:native   # → core/build-native.ps1
```

### Web UI（`client/web/`）

| 脚本 | 作用 |
|------|------|
| [`start.ps1`](web/start.ps1) / [`start.sh`](web/start.sh) | 仅启动 dev（假定已构建） |
| [`scripts/copy-wasm.mjs`](web/scripts/copy-wasm.mjs) | 复制 WASM 到 `public/wasm/`（`predev` / `prebuild` 自动调用） |

### WASM / Emscripten（`client/core/twelve_c_wasm/`）

| 脚本 | 作用 |
|------|------|
| [`setup-emsdk.ps1`](core/twelve_c_wasm/setup-emsdk.ps1) / [`.sh`](core/twelve_c_wasm/setup-emsdk.sh) | 一次性安装 emsdk |
| [`build-wasm.ps1`](core/twelve_c_wasm/build-wasm.ps1) / [`.sh`](core/twelve_c_wasm/build-wasm.sh) | 编译 WASM → `transfer/src/wasm/pkg/` |
| [`build-openssl-emscripten.ps1`](core/twelve_c_wasm/build-openssl-emscripten.ps1) / [`.sh`](core/twelve_c_wasm/build-openssl-emscripten.sh) | 交叉编译 OpenSSL（通常由 build-wasm 自动调用） |

### 原生 C++ 验证（`client/core/`）

| 脚本 | 作用 |
|------|------|
| [`build-native.ps1`](core/build-native.ps1) / [`.sh`](core/build-native.sh) | CMake 编译 → 仓库根 `build-test/native/` |

### Transfer npm 快捷方式（`client/transfer/`）

与 `twelve_c_wasm` 脚本等价，可在 `client/transfer` 目录执行：

- `npm run build:wasm` / `build:wasm:sh`
- `npm run setup:emsdk` / `setup:emsdk:sh`
- `npm run build:openssl:wasm` / `build:openssl:wasm:sh`

## 快速开始

```powershell
cd client
.\build.ps1 -SetupEmsdk -EmsdkRoot P:\_Tools\emsdk   # 首次
.\start.ps1                                           # 日常
```

完整联调说明见仓库根目录 [`HOW_TO_SETUP.md`](../HOW_TO_SETUP.md)。

## 桌面安装包

仓库 CI 会先构建 WASM 与 Web，再分别产出 Windows NSIS 安装包和 Linux
AppImage。下载对应 `build-and-test` 工作流运行中的
`12c-desktop-windows` / `12c-desktop-linux` artifact 即可。

本地已有 `web/dist` 时，也可以执行：

```bash
npm run electron:build:win
npm run electron:build:linux
```

桌面壳默认加载打包内的 Web 静态资源；设置 `TWELVE_C_APP_URL` 后可改为加载
指定的 HTTPS 部署地址。桌面窗口禁用了 Node 集成、导航跳转和新窗口，并启用
context isolation 与 Chromium sandbox。
