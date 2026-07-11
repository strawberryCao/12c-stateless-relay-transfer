import {
  DEFAULT_MAX_RESERVATION_ATTEMPTS,
  formatCredentialStyleLabel,
  generateCredential,
  receiveFile,
  UploadTokenReservationExhaustedError,
  validateCredentialStyle,
  type CredentialStyleOptions,
} from '@stateless-relay/app';
import {
  CREDENTIAL_LENGTH,
  prepareUpload,
  RegistryTokenOccupiedError,
  resolveRegistryBaseUrl,
  uploadPrepared,
  type OccupiedTokenInfo,
  type UploadProgress,
  type UploadReservationMeta,
} from '@stateless-relay/transfer';
import type { ClientRuntime } from './runtime.js';
import {
  bootstrapClientRuntime,
  clearStoredRegistryUrl,
  getEffectiveFileTtlSeconds,
  loadEffectiveConfig,
  saveStoredFileTtlSeconds,
  saveStoredRegistryUrl,
} from './runtime.js';
import { CredentialSlotDisplay } from './credential-slots.js';
import { renderFileIconForFile, renderFileIconForName } from './file-type-icon.js';
import { ReceiveCredentialInput } from './receive-credential-input.js';
import { clearBanner, initBanners, showBanner } from './banner.js';
import {
  clampDurationParts,
  durationPartsFromSeconds,
  formatDurationLabel,
} from './file-ttl.js';
import {
  getEffectiveCredentialStyle,
  saveStoredCredentialStyle,
} from './credential-style-settings.js';

// ============================================================================
// 类型定义
// ============================================================================

type PanelName = 'send' | 'receive' | 'settings';

interface Elements {
  bootScreen: HTMLElement;
  bootStatus: HTMLElement;
  bootProgressBar: HTMLElement;
  app: HTMLElement;
  registryUrlLabel: HTMLElement;
  navItems: NodeListOf<HTMLButtonElement>;
  panels: NodeListOf<HTMLElement>;
  sendFileInput: HTMLInputElement;
  sendFileDropzone: HTMLLabelElement;
  sendFileEmpty: HTMLElement;
  sendFileSelected: HTMLElement;
  sendFileName: HTMLElement;
  sendFileSize: HTMLElement;
  sendFileIcon: HTMLElement;
  sendUploadStamp: HTMLElement;
  sendBtn: HTMLButtonElement;
  sendError: HTMLElement;
  sendInfo: HTMLElement;
  sendProgressText: HTMLElement;
  sendProgressBar: HTMLElement;
  sendCredentialSlots: HTMLElement;
  copyCredentialBtn: HTMLButtonElement;
  receiveCredentialSlots: HTMLElement;
  receiveDownloadBtn: HTMLButtonElement;
  receiveDownloadHint: HTMLElement;
  receiveFileBar: HTMLElement;
  receiveFilePending: HTMLElement;
  receiveFileReady: HTMLElement;
  receiveFileIcon: HTMLElement;
  receiveFileName: HTMLElement;
  receiveFileSize: HTMLElement;
  receiveFileSaveHint: HTMLElement;
  receiveError: HTMLElement;
  registryUrlInput: HTMLInputElement;
  saveSettingsBtn: HTMLButtonElement;
  resetSettingsBtn: HTMLButtonElement;
  fileTtlHoursInput: HTMLInputElement;
  fileTtlMinutesInput: HTMLInputElement;
  fileTtlSecondsInput: HTMLInputElement;
  confirmFileTtlBtn: HTMLButtonElement;
  fileTtlCurrent: HTMLElement;
  credentialStyleUppercase: HTMLInputElement;
  credentialStyleLowercase: HTMLInputElement;
  credentialStyleWord: HTMLInputElement;
  credentialStyleHyphen: HTMLInputElement;
  credentialStyleLettersLastSix: HTMLInputElement;
  credentialStyleLettersFirstSix: HTMLInputElement;
  confirmCredentialStyleBtn: HTMLButtonElement;
  credentialStyleCurrent: HTMLElement;
  settingsError: HTMLElement;
  settingsInfo: HTMLElement;
}

// ============================================================================
// 全局状态
// ============================================================================

let runtime: ClientRuntime | null = null;
let selectedFile: File | null = null;
let activePanel: PanelName = 'send';
let credentialDisplay: CredentialSlotDisplay | null = null;
let receiveCredentialInput: ReceiveCredentialInput | null = null;
let receivedFile: { fileName: string; data: Uint8Array } | null = null;
let receiveDownloading = false;
let sendInProgress = false;

// ============================================================================
// 工具函数
// ============================================================================

/** 防抖：限制高频调用，默认延迟 150ms */
function debounce<T extends (...args: never[]) => void>(
  fn: T,
  delayMs = 150,
): (...args: Parameters<T>) => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return (...args: Parameters<T>) => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, delayMs);
  };
}

type ReceiveDownloadState = 'idle' | 'ready' | 'downloading' | 'done';

const RECEIVE_DOWNLOAD_LABELS: Record<ReceiveDownloadState, string> = {
  idle: '点击下载',
  ready: '点击下载',
  downloading: '下载中',
  done: '已下载',
};

// ============================================================================
// DOM 元素收集
// ============================================================================

function $(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (!node) {
    throw new Error(`missing element #${id}`);
  }
  return node;
}

function collectElements(): Elements {
  return {
    bootScreen: $('boot-screen'),
    bootStatus: $('boot-status'),
    bootProgressBar: $('boot-progress-bar'),
    app: $('app'),
    registryUrlLabel: $('registry-url-label'),
    navItems: document.querySelectorAll<HTMLButtonElement>('.nav-item'),
    panels: document.querySelectorAll<HTMLElement>('.panel'),
    sendFileInput: $('send-file-input') as HTMLInputElement,
    sendFileDropzone: $('send-file-dropzone') as HTMLLabelElement,
    sendFileEmpty: $('send-file-empty'),
    sendFileSelected: $('send-file-selected'),
    sendFileName: $('send-file-name'),
    sendFileSize: $('send-file-size'),
    sendFileIcon: $('send-file-icon'),
    sendUploadStamp: $('send-upload-stamp'),
    sendBtn: $('send-btn') as HTMLButtonElement,
    sendError: $('send-error'),
    sendInfo: $('send-info'),
    sendProgressText: $('send-progress-text'),
    sendProgressBar: $('send-progress-bar'),
    sendCredentialSlots: $('send-credential-slots'),
    copyCredentialBtn: $('copy-credential-btn') as HTMLButtonElement,
    receiveCredentialSlots: $('receive-credential-slots'),
    receiveDownloadBtn: $('receive-download-btn') as HTMLButtonElement,
    receiveDownloadHint: $('receive-download-hint'),
    receiveFileBar: $('receive-file-bar'),
    receiveFilePending: $('receive-file-pending'),
    receiveFileReady: $('receive-file-ready'),
    receiveFileIcon: $('receive-file-icon'),
    receiveFileName: $('receive-file-name'),
    receiveFileSize: $('receive-file-size'),
    receiveFileSaveHint: $('receive-file-save-hint'),
    receiveError: $('receive-error'),
    registryUrlInput: $('registry-url-input') as HTMLInputElement,
    saveSettingsBtn: $('save-settings-btn') as HTMLButtonElement,
    resetSettingsBtn: $('reset-settings-btn') as HTMLButtonElement,
    fileTtlHoursInput: $('file-ttl-hours') as HTMLInputElement,
    fileTtlMinutesInput: $('file-ttl-minutes') as HTMLInputElement,
    fileTtlSecondsInput: $('file-ttl-seconds') as HTMLInputElement,
    confirmFileTtlBtn: $('confirm-file-ttl-btn') as HTMLButtonElement,
    fileTtlCurrent: $('file-ttl-current'),
    credentialStyleUppercase: $('credential-style-uppercase') as HTMLInputElement,
    credentialStyleLowercase: $('credential-style-lowercase') as HTMLInputElement,
    credentialStyleWord: $('credential-style-word') as HTMLInputElement,
    credentialStyleHyphen: $('credential-style-hyphen') as HTMLInputElement,
    credentialStyleLettersLastSix: $('credential-style-letters-last-six') as HTMLInputElement,
    credentialStyleLettersFirstSix: $('credential-style-letters-first-six') as HTMLInputElement,
    confirmCredentialStyleBtn: $('confirm-credential-style-btn') as HTMLButtonElement,
    credentialStyleCurrent: $('credential-style-current'),
    settingsError: $('settings-error'),
    settingsInfo: $('settings-info'),
  };
}

const el = collectElements();

function formatBytes(size: number): string {
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / (1024 * 1024)).toFixed(2)} MB`;
}

function setBootProgress(ratio: number, message: string): void {
  el.bootStatus.textContent = message;
  el.bootProgressBar.style.width = `${Math.max(0, Math.min(100, ratio * 100))}%`;
}

function showApp(): void {
  el.bootScreen.classList.add('hidden');
  el.app.classList.remove('hidden');
}

// ============================================================================
// 导航与面板切换
// ============================================================================

function switchPanel(panel: PanelName): void {
  activePanel = panel;
  el.navItems.forEach((item) => {
    item.classList.toggle('active', item.dataset.panel === panel);
  });
  el.panels.forEach((section) => {
    section.classList.toggle('active', section.id === `panel-${panel}`);
  });
}

function updateRegistryLabel(): void {
  el.registryUrlLabel.textContent = runtime?.registryUrl ?? '—';
}

// ============================================================================
// 运行时与启动
// ============================================================================

async function reloadRuntime(): Promise<void> {
  setBootProgress(0.35, '正在连接 Registry…');
  runtime = await bootstrapClientRuntime();
  updateRegistryLabel();
  el.registryUrlInput.value = runtime.registryUrl;
}

function requireRuntime(): ClientRuntime {
  if (!runtime) {
    throw new Error('客户端尚未就绪');
  }
  return runtime;
}

// ============================================================================
// 发送面板 - 文件选择与 UI 状态
// ============================================================================

function clearUploadStamp(): void {
  el.sendUploadStamp.classList.remove('visible');
  el.sendUploadStamp.classList.add('hidden');
  el.sendFileDropzone.classList.remove('is-uploaded');
}

function showUploadStamp(): void {
  el.sendUploadStamp.classList.remove('hidden');
  requestAnimationFrame(() => {
    el.sendUploadStamp.classList.add('visible');
  });
  el.sendFileDropzone.classList.add('is-uploaded');
}

function updateSendControls(): void {
  el.sendBtn.disabled = sendInProgress || !selectedFile;
  el.sendFileInput.disabled = sendInProgress;
  el.sendFileDropzone.classList.toggle('is-disabled', sendInProgress);
  el.sendFileDropzone.toggleAttribute('aria-disabled', sendInProgress);
}

function updateSendFilePicker(): void {
  if (!selectedFile) {
    el.sendFileDropzone.classList.remove('has-file');
    el.sendFileEmpty.classList.remove('hidden');
    el.sendFileSelected.classList.add('hidden');
    clearUploadStamp();
    updateSendControls();
    return;
  }

  clearUploadStamp();
  el.sendFileDropzone.classList.add('has-file');
  el.sendFileEmpty.classList.add('hidden');
  el.sendFileSelected.classList.remove('hidden');
  renderFileIconForFile(el.sendFileIcon, selectedFile);
  el.sendFileName.textContent = selectedFile.name;
  el.sendFileSize.textContent = formatBytes(selectedFile.size);
  updateSendControls();
}

function resetSendProgress(): void {
  el.sendProgressBar.style.width = '0%';
  el.sendProgressText.textContent = '';
}

function resetSendCredential(): void {
  credentialDisplay?.reset();
  el.copyCredentialBtn.disabled = true;
}

function resetSendPanel(): void {
  resetSendCredential();
  resetSendProgress();
  clearUploadStamp();
}

function formatUploadReservationDegradedMessage(
  reservation: UploadReservationMeta,
): string {
  const parts: string[] = [];
  if (reservation.grantedTtlSeconds < reservation.requestedTtlSeconds) {
    parts.push(
      `上传有效期已由 ${formatDurationLabel(reservation.requestedTtlSeconds)} 调整为 ${formatDurationLabel(reservation.grantedTtlSeconds)}`,
    );
  }
  const plan = reservation.placementPlan;
  if (plan && plan.relayCount < plan.idealRelayCount) {
    parts.push(
      `备份/分散布局已降级（${plan.relayCount}/${plan.idealRelayCount} 台 Relay）`,
    );
  }
  if (parts.length === 0) {
    return 'Registry 已降级上传分配策略（当前 Relay 存储能力或冗余不足）。';
  }
  return `${parts.join('；')}（当前 Relay 存储能力或冗余不足）。`;
}

// ============================================================================
// 发送面板 - 上传流程
// ============================================================================

async function handleSend(): Promise<void> {
  clearBanner(el.sendError);
  clearBanner(el.sendInfo);
  resetSendPanel();

  if (!selectedFile) {
    showBanner(el.sendError, '请先选择文件');
    return;
  }

  const client = requireRuntime();
  const display = credentialDisplay;
  if (!display) {
    throw new Error('credential display not initialized');
  }

  el.copyCredentialBtn.disabled = true;
  sendInProgress = true;
  updateSendControls();
  display.startBreathing();

  const bytesPromise = selectedFile
    .arrayBuffer()
    .then((buffer) => new Uint8Array(buffer));
  await display.enterReelWhenReady(bytesPromise);
  const bytes = await bytesPromise;

  const maxAttempts = DEFAULT_MAX_RESERVATION_ATTEMPTS;
  let lastOccupiedTokens: OccupiedTokenInfo[] = [];

  try {
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      const credential = generateCredential(getEffectiveCredentialStyle());

      try {
        const uploads = prepareUpload(
          bytes,
          credential,
          client.twelveC,
          selectedFile.name,
        );
        await display.revealSequential(credential);
        el.copyCredentialBtn.disabled = false;

        const { replicaSync, reservation } = await uploadPrepared(
          uploads,
          client.stack.router,
          client.stack.uploadClient,
          {
            registry: client.stack.registry,
            ttlSeconds: getEffectiveFileTtlSeconds(),
          },
          (progress: UploadProgress) => {
            const ratio = progress.total > 0 ? progress.completed / progress.total : 0;
            el.sendProgressBar.style.width = `${Math.round(ratio * 100)}%`;
            el.sendProgressText.textContent = `${progress.completed} / ${progress.total}`;
          },
        );

        if (reservation?.degraded) {
          showBanner(
            el.sendInfo,
            formatUploadReservationDegradedMessage(reservation),
          );
        }

        showUploadStamp();
        void replicaSync?.catch(() => {
          /* replica 后台补传失败不影响主流程 */
        });
        return;
      } catch (error) {
        if (!(error instanceof RegistryTokenOccupiedError)) {
          throw error;
        }

        lastOccupiedTokens = (error as RegistryTokenOccupiedError).occupiedTokens;
        display.startReelSpinning();

        if (attempt < maxAttempts) {
          continue;
        }
      }
    }

    throw new UploadTokenReservationExhaustedError(maxAttempts, lastOccupiedTokens);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    showBanner(el.sendError, message);
    if (!display.getValue()) {
      display.reset();
    }
  } finally {
    sendInProgress = false;
    updateSendControls();
  }
}

// ============================================================================
// 接收面板 - 文件下载与保存
// ============================================================================

function saveBlob(filename: string, data: Uint8Array): void {
  const copy = new Uint8Array(data);
  const blob = new Blob([copy], { type: 'application/octet-stream' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function resetReceiveFileBar(): void {
  receivedFile = null;
  el.receiveFileBar.classList.remove('receive-file-bar--ready');
  el.receiveFileBar.classList.add('receive-file-bar--pending');
  el.receiveFileBar.classList.remove('is-clickable');
  el.receiveFilePending.classList.remove('hidden');
  el.receiveFileReady.classList.add('hidden');
  el.receiveFileSaveHint.classList.add('hidden');
  el.receiveFileIcon.replaceChildren();
  el.receiveFileName.textContent = '';
  el.receiveFileSize.textContent = '';
}

function setReceiveDownloadVisual(state: ReceiveDownloadState): void {
  el.receiveDownloadBtn.classList.remove(
    'receive-download-trigger--idle',
    'receive-download-trigger--ready',
    'receive-download-trigger--downloading',
    'receive-download-trigger--done',
  );
  el.receiveDownloadBtn.classList.add(`receive-download-trigger--${state}`);
  el.receiveDownloadHint.textContent = RECEIVE_DOWNLOAD_LABELS[state];
  el.receiveDownloadBtn.setAttribute('aria-label', RECEIVE_DOWNLOAD_LABELS[state]);
}

function updateReceiveDownloadButton(): void {
  if (receiveDownloading) {
    setReceiveDownloadVisual('downloading');
    el.receiveDownloadBtn.disabled = true;
    return;
  }
  if (receivedFile) {
    setReceiveDownloadVisual('done');
    el.receiveDownloadBtn.disabled = true;
    return;
  }
  const complete = receiveCredentialInput?.isComplete() === true;
  setReceiveDownloadVisual(complete ? 'ready' : 'idle');
  el.receiveDownloadBtn.disabled = !complete;
}

function showReceivedFile(fileName: string, data: Uint8Array): void {
  receivedFile = { fileName, data };
  el.receiveFileBar.classList.remove('receive-file-bar--pending');
  el.receiveFileBar.classList.add('receive-file-bar--ready', 'is-clickable');
  el.receiveFilePending.classList.add('hidden');
  el.receiveFileReady.classList.remove('hidden');
  el.receiveFileSaveHint.classList.remove('hidden');
  renderFileIconForName(el.receiveFileIcon, fileName);
  el.receiveFileName.textContent = fileName;
  el.receiveFileSize.textContent = formatBytes(data.byteLength);
  updateReceiveDownloadButton();
}

function handleSaveReceivedFile(): void {
  if (!receivedFile) {
    return;
  }
  saveBlob(receivedFile.fileName, receivedFile.data);
}

async function handleReceiveDownload(): Promise<void> {
  clearBanner(el.receiveError);
  const credential = receiveCredentialInput?.getValue() ?? '';
  if (credential.length !== CREDENTIAL_LENGTH) {
    showBanner(el.receiveError, `凭证必须为 ${CREDENTIAL_LENGTH} 位`);
    return;
  }

  const client = requireRuntime();
  receiveDownloading = true;
  updateReceiveDownloadButton();

  try {
    const received = await receiveFile(credential, {
      twelveC: client.twelveC,
      receiveTransport: client.stack.receiveTransport,
    });
    showReceivedFile(received.fileName, received.data);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    showBanner(el.receiveError, message);
  } finally {
    receiveDownloading = false;
    updateReceiveDownloadButton();
  }
}

// ============================================================================
// 设置面板 - 文件有效期 (TTL)
// ============================================================================

function applyDurationPartsToInputs(parts: ReturnType<typeof durationPartsFromSeconds>): void {
  el.fileTtlHoursInput.value = String(parts.hours);
  el.fileTtlMinutesInput.value = String(parts.minutes);
  el.fileTtlSecondsInput.value = String(parts.seconds);
}

function updateFileTtlCurrentLabel(totalSeconds: number): void {
  el.fileTtlCurrent.textContent = formatDurationLabel(totalSeconds);
}

function syncFileTtlSettingsUi(): void {
  const totalSeconds = getEffectiveFileTtlSeconds();
  applyDurationPartsToInputs(durationPartsFromSeconds(totalSeconds));
  updateFileTtlCurrentLabel(totalSeconds);
}

function readDurationInputs(): ReturnType<typeof clampDurationParts> {
  return clampDurationParts(
    Number(el.fileTtlHoursInput.value),
    Number(el.fileTtlMinutesInput.value),
    Number(el.fileTtlSecondsInput.value),
  );
}

function handleConfirmFileTtl(): void {
  clearBanner(el.settingsError);
  clearBanner(el.settingsInfo);

  const parts = readDurationInputs();
  if (parts.totalSeconds <= 0) {
    showBanner(el.settingsError, '文件有效时间必须大于 0 秒');
    return;
  }

  applyDurationPartsToInputs(parts);
  saveStoredFileTtlSeconds(parts.totalSeconds);
  updateFileTtlCurrentLabel(parts.totalSeconds);
  showBanner(el.settingsInfo, '文件有效时间已更新。');
}

// ============================================================================
// 设置面板 - 凭证风格
// ============================================================================

function lettersEnabledForCredentialStyle(): boolean {
  return el.credentialStyleUppercase.checked || el.credentialStyleLowercase.checked;
}

function updateWordStyleCheckboxState(): void {
  const enabled = lettersEnabledForCredentialStyle();
  el.credentialStyleWord.disabled = !enabled;
  if (!enabled) {
    el.credentialStyleWord.checked = false;
  }
}

function readCredentialStyleFromInputs(): CredentialStyleOptions {
  const lettersOnlyFirstSix = el.credentialStyleLettersFirstSix.checked;
  const lettersOnlyLastSix =
    el.credentialStyleLettersLastSix.checked && !lettersOnlyFirstSix;
  const includeUppercase = el.credentialStyleUppercase.checked;
  const includeLowercase = el.credentialStyleLowercase.checked;

  return {
    includeUppercase,
    includeLowercase,
    allowAtMostOneHyphen: el.credentialStyleHyphen.checked,
    lettersOnlyLastSix,
    lettersOnlyFirstSix,
    wordStyle:
      el.credentialStyleWord.checked && (includeUppercase || includeLowercase),
  };
}

function applyCredentialStyleToInputs(options: CredentialStyleOptions): void {
  const lettersOnlyFirstSix = options.lettersOnlyFirstSix;
  const lettersOnlyLastSix =
    options.lettersOnlyLastSix && !lettersOnlyFirstSix;

  el.credentialStyleUppercase.checked = options.includeUppercase;
  el.credentialStyleLowercase.checked = options.includeLowercase;
  el.credentialStyleHyphen.checked = options.allowAtMostOneHyphen;
  el.credentialStyleWord.checked = options.wordStyle;
  el.credentialStyleLettersLastSix.checked = lettersOnlyLastSix;
  el.credentialStyleLettersFirstSix.checked = lettersOnlyFirstSix;
  updateWordStyleCheckboxState();
}

function updateCredentialStyleCurrentLabel(options: CredentialStyleOptions): void {
  el.credentialStyleCurrent.textContent = formatCredentialStyleLabel(options);
}

function syncCredentialStyleSettingsUi(): void {
  const options = getEffectiveCredentialStyle();
  applyCredentialStyleToInputs(options);
  updateCredentialStyleCurrentLabel(options);
}

function handleConfirmCredentialStyle(): void {
  clearBanner(el.settingsError);
  clearBanner(el.settingsInfo);

  const options = readCredentialStyleFromInputs();
  const validationError = validateCredentialStyle(options);
  if (validationError) {
    showBanner(el.settingsError, validationError);
    return;
  }

  saveStoredCredentialStyle(options);
  updateCredentialStyleCurrentLabel(options);
  showBanner(el.settingsInfo, '凭证风格已更新。');
}

// ============================================================================
// 设置面板 - Registry 连接
// ============================================================================

async function handleSaveSettings(): Promise<void> {
  clearBanner(el.settingsError);
  clearBanner(el.settingsInfo);

  const url = el.registryUrlInput.value.trim();
  if (!url) {
    showBanner(el.settingsError, 'Registry URL 不能为空');
    return;
  }

  saveStoredRegistryUrl(url);
  el.bootScreen.classList.remove('hidden');
  el.app.classList.add('hidden');
  setBootProgress(0.2, '正在重连 Registry…');

  try {
    await reloadRuntime();
    showApp();
    showBanner(el.settingsInfo, '已保存并重连 Registry。');
    if (activePanel !== 'settings') {
      switchPanel('settings');
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    showBanner(el.settingsError, message);
    showApp();
  }
}

async function handleResetSettings(): Promise<void> {
  clearBanner(el.settingsError);
  clearBanner(el.settingsInfo);
  clearStoredRegistryUrl();

  const config = await loadEffectiveConfig();
  el.registryUrlInput.value = resolveRegistryBaseUrl(config.registry);
  showBanner(el.settingsInfo, '已恢复默认（尚未重连，请点击保存并重连）。');
}

async function copyCredential(): Promise<void> {
  const value = credentialDisplay?.getValue();
  if (!value) {
    return;
  }
  await navigator.clipboard.writeText(value);
  showBanner(el.sendInfo, '凭证已复制到剪贴板。');
}

// ============================================================================
// 事件绑定
// ============================================================================

function bindEvents(): void {
  el.navItems.forEach((item) => {
    item.addEventListener('click', () => {
      const panel = item.dataset.panel as PanelName | undefined;
      if (panel) {
        switchPanel(panel);
      }
    });
  });

  el.sendFileInput.addEventListener('change', () => {
    if (sendInProgress) {
      return;
    }
    selectedFile = el.sendFileInput.files?.[0] ?? null;
    updateSendFilePicker();
    resetSendPanel();
    clearBanner(el.sendError);
    clearBanner(el.sendInfo);
  });

  el.sendBtn.addEventListener('click', () => {
    if (sendInProgress) {
      return;
    }
    void handleSend();
  });

  el.receiveDownloadBtn.addEventListener('click', () => {
    void handleReceiveDownload();
  });

  el.receiveFileBar.addEventListener('click', () => {
    handleSaveReceivedFile();
  });

  el.saveSettingsBtn.addEventListener('click', () => {
    void handleSaveSettings();
  });

  el.resetSettingsBtn.addEventListener('click', () => {
    void handleResetSettings();
  });

  el.confirmFileTtlBtn.addEventListener('click', () => {
    handleConfirmFileTtl();
  });

  el.confirmCredentialStyleBtn.addEventListener('click', () => {
    handleConfirmCredentialStyle();
  });

  el.credentialStyleLettersFirstSix.addEventListener('change', () => {
    if (el.credentialStyleLettersFirstSix.checked) {
      el.credentialStyleLettersLastSix.checked = false;
    }
  });

  el.credentialStyleLettersLastSix.addEventListener('change', () => {
    if (el.credentialStyleLettersLastSix.checked) {
      el.credentialStyleLettersFirstSix.checked = false;
    }
  });

  for (const input of [el.credentialStyleUppercase, el.credentialStyleLowercase]) {
    input.addEventListener('change', () => {
      updateWordStyleCheckboxState();
    });
  }

  const debouncedSyncTtl = debounce(() => {
    const parts = readDurationInputs();
    applyDurationPartsToInputs(parts);
  }, 200);

  for (const input of [el.fileTtlHoursInput, el.fileTtlMinutesInput, el.fileTtlSecondsInput]) {
    input.addEventListener('input', () => {
      debouncedSyncTtl();
    });
    input.addEventListener('change', () => {
      const parts = readDurationInputs();
      applyDurationPartsToInputs(parts);
    });
  }

  el.copyCredentialBtn.addEventListener('click', () => {
    void copyCredential();
  });
}

export async function startApp(): Promise<void> {
  initBanners();
  bindEvents();
  credentialDisplay = new CredentialSlotDisplay(el.sendCredentialSlots);
  receiveCredentialInput = new ReceiveCredentialInput(el.receiveCredentialSlots);
  receiveCredentialInput.onChange(() => {
    resetReceiveFileBar();
    updateReceiveDownloadButton();
    clearBanner(el.receiveError);
  });
  resetReceiveFileBar();
  updateReceiveDownloadButton();
  syncFileTtlSettingsUi();
  syncCredentialStyleSettingsUi();
  setBootProgress(0.1, '正在加载加密模块…');

  try {
    setBootProgress(0.25, '正在读取配置…');
    await reloadRuntime();
    setBootProgress(1, '就绪');
    showApp();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    el.bootStatus.textContent = `启动失败：${message}`;
    el.bootProgressBar.style.width = '0%';
  }
}
