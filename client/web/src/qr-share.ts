import qrcode from 'qrcode-generator';
import { CREDENTIAL_LENGTH } from '@stateless-relay/transfer';

const CREDENTIAL_PATTERN = /^[A-Za-z0-9-]+$/;
const STYLE_ELEMENT_ID = 'qr-share-feature-styles';

interface ShareElements {
  section: HTMLElement;
  qrContainer: HTMLElement;
  linkOutput: HTMLInputElement;
  copyLinkButton: HTMLButtonElement;
  downloadQrButton: HTMLButtonElement;
  receiveInfo: HTMLElement;
}

interface ShareState {
  credential: string;
  receiveUrl: string;
}

function isValidCredential(value: string): boolean {
  return value.length === CREDENTIAL_LENGTH && CREDENTIAL_PATTERN.test(value);
}

function buildReceiveUrl(credential: string): string {
  const url = new URL(window.location.href);
  url.search = '';
  const fragment = new URLSearchParams();
  fragment.set('receive', credential);
  url.hash = fragment.toString();
  return url.toString();
}

function readCredentialFromHash(): string | null {
  const hash = window.location.hash.startsWith('#')
    ? window.location.hash.slice(1)
    : window.location.hash;
  if (!hash) {
    return null;
  }

  const value = new URLSearchParams(hash).get('receive')?.trim() ?? '';
  return isValidCredential(value) ? value : null;
}

function readCredentialFromSendSlots(): string | null {
  const container = document.getElementById('send-credential-slots');
  if (!container) {
    return null;
  }

  const value = Array.from(container.querySelectorAll<HTMLElement>('.credential-slot'))
    .map((slot) => slot.textContent ?? '')
    .join('');
  return isValidCredential(value) ? value : null;
}

function createQrSvg(receiveUrl: string): string {
  const qr = qrcode(0, 'M');
  qr.addData(receiveUrl, 'Byte');
  qr.make();
  return qr.createSvgTag({ cellSize: 8, margin: 32, scalable: true });
}

function installStyles(): void {
  if (document.getElementById(STYLE_ELEMENT_ID)) {
    return;
  }

  const style = document.createElement('style');
  style.id = STYLE_ELEMENT_ID;
  style.textContent = `
    .send-share-section {
      margin-top: 1.25rem;
      padding-top: 1.25rem;
      border-top: 1px solid var(--border);
    }

    .send-share-header {
      margin-bottom: 0.9rem;
    }

    .send-share-title {
      margin: 0;
      color: var(--text-strong);
      font-size: 1rem;
    }

    .send-share-subtitle {
      margin: 0.25rem 0 0;
      color: var(--muted);
      font-size: 0.85rem;
    }

    .send-share-grid {
      display: grid;
      grid-template-columns: minmax(12rem, 15rem) minmax(0, 1fr);
      gap: 1rem;
      align-items: center;
    }

    .send-share-qr {
      width: 100%;
      aspect-ratio: 1;
      display: grid;
      place-items: center;
      padding: 0.75rem;
      background: #fff;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow-sm);
    }

    .send-share-qr svg {
      display: block;
      width: 100%;
      height: auto;
      max-width: 100%;
    }

    .send-share-details {
      min-width: 0;
    }

    .send-share-link-label {
      display: block;
      margin-bottom: 0.45rem;
      color: var(--muted);
      font-size: 0.85rem;
    }

    .send-share-link {
      width: 100%;
      margin-bottom: 0.75rem;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 0.82rem;
    }

    .send-share-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.6rem;
    }

    .send-share-security-note {
      margin: 0.75rem 0 0;
      color: var(--muted);
      font-size: 0.82rem;
      line-height: 1.5;
    }

    .receive-link-info {
      margin-bottom: 1rem;
    }

    @media (max-width: 720px) {
      .send-share-grid {
        grid-template-columns: 1fr;
      }

      .send-share-qr {
        width: min(17rem, 100%);
        justify-self: center;
      }

      .send-share-actions > button {
        flex: 1 1 10rem;
      }
    }
  `;
  document.head.appendChild(style);
}

function createShareElements(): ShareElements {
  const credentialSection = document.querySelector<HTMLElement>('.send-credential-section');
  if (!credentialSection) {
    throw new Error('missing send credential section');
  }

  let section = document.getElementById('send-share-section');
  if (!section) {
    section = document.createElement('section');
    section.id = 'send-share-section';
    section.className = 'send-share-section hidden';
    section.setAttribute('aria-live', 'polite');
    section.innerHTML = `
      <div class="send-share-header">
        <h2 class="send-share-title">扫码接收文件</h2>
        <p class="send-share-subtitle">接收方扫码后会自动打开接收页并填入 12 位凭证。</p>
      </div>
      <div class="send-share-grid">
        <div id="send-share-qr" class="send-share-qr" aria-label="文件接收二维码"></div>
        <div class="send-share-details">
          <label class="send-share-link-label" for="send-share-link-output">接收链接</label>
          <input id="send-share-link-output" class="send-share-link" type="text" readonly />
          <div class="send-share-actions">
            <button id="copy-receive-link-btn" type="button" class="primary-btn">复制接收链接</button>
            <button id="download-receive-qr-btn" type="button" class="ghost-btn">下载二维码</button>
          </div>
          <p class="send-share-security-note">二维码包含文件提取凭证，请只发送给接收方。</p>
        </div>
      </div>
    `;
    credentialSection.insertAdjacentElement('afterend', section);
  }

  const receiveError = document.getElementById('receive-error');
  if (!receiveError) {
    throw new Error('missing receive error banner');
  }

  let receiveInfo = document.getElementById('receive-link-info');
  if (!receiveInfo) {
    receiveInfo = document.createElement('div');
    receiveInfo.id = 'receive-link-info';
    receiveInfo.className = 'banner info receive-link-info hidden';
    receiveError.insertAdjacentElement('afterend', receiveInfo);
  }

  const qrContainer = document.getElementById('send-share-qr');
  const linkOutput = document.getElementById('send-share-link-output');
  const copyLinkButton = document.getElementById('copy-receive-link-btn');
  const downloadQrButton = document.getElementById('download-receive-qr-btn');

  if (
    !qrContainer ||
    !(linkOutput instanceof HTMLInputElement) ||
    !(copyLinkButton instanceof HTMLButtonElement) ||
    !(downloadQrButton instanceof HTMLButtonElement)
  ) {
    throw new Error('failed to create QR share controls');
  }

  return {
    section,
    qrContainer,
    linkOutput,
    copyLinkButton,
    downloadQrButton,
    receiveInfo,
  };
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) {
    throw new Error('浏览器未允许复制，请手动复制接收链接。');
  }
}

function applyReceiveCredential(credential: string, receiveInfo: HTMLElement): void {
  const receiveNav = document.querySelector<HTMLButtonElement>('[data-panel="receive"]');
  receiveNav?.click();

  const inputs = Array.from(
    document.querySelectorAll<HTMLInputElement>('#receive-credential-slots input'),
  );
  if (inputs.length !== CREDENTIAL_LENGTH) {
    throw new Error('接收凭证输入框尚未初始化');
  }

  for (let index = 0; index < inputs.length; index++) {
    inputs[index]!.value = credential[index] ?? '';
  }
  inputs[inputs.length - 1]!.dispatchEvent(new Event('input', { bubbles: true }));

  receiveInfo.textContent = '已从扫码链接填入凭证，请点击“下载”获取并解密文件。';
  receiveInfo.classList.remove('hidden');
  document.getElementById('receive-download-btn')?.focus();
}

function downloadQrSvg(state: ShareState, qrContainer: HTMLElement): void {
  const svg = qrContainer.querySelector('svg');
  if (!svg) {
    return;
  }

  const source = `<?xml version="1.0" encoding="UTF-8"?>\n${svg.outerHTML}`;
  const blob = new Blob([source], { type: 'image/svg+xml;charset=utf-8' });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = `12C-${state.credential}-接收二维码.svg`;
  anchor.click();
  URL.revokeObjectURL(objectUrl);
}

export function installQrShareFeature(): void {
  installStyles();
  const elements = createShareElements();
  const uploadStamp = document.getElementById('send-upload-stamp');
  const sendFileInput = document.getElementById('send-file-input');
  if (!uploadStamp) {
    throw new Error('missing upload success marker');
  }

  let state: ShareState | null = null;

  const hideShare = (): void => {
    state = null;
    elements.section.classList.add('hidden');
    elements.qrContainer.replaceChildren();
    elements.linkOutput.value = '';
  };

  const showShareForCompletedUpload = (): void => {
    if (uploadStamp.classList.contains('hidden')) {
      hideShare();
      return;
    }

    const credential = readCredentialFromSendSlots();
    if (!credential) {
      hideShare();
      return;
    }

    const receiveUrl = buildReceiveUrl(credential);
    state = { credential, receiveUrl };
    elements.linkOutput.value = receiveUrl;
    elements.qrContainer.innerHTML = createQrSvg(receiveUrl);
    elements.section.classList.remove('hidden');
  };

  new MutationObserver(showShareForCompletedUpload).observe(uploadStamp, {
    attributes: true,
    attributeFilter: ['class'],
  });
  sendFileInput?.addEventListener('change', hideShare);

  elements.copyLinkButton.addEventListener('click', () => {
    if (!state) {
      return;
    }

    void copyText(state.receiveUrl)
      .then(() => {
        elements.copyLinkButton.textContent = '已复制';
        window.setTimeout(() => {
          elements.copyLinkButton.textContent = '复制接收链接';
        }, 1600);
      })
      .catch((error: unknown) => {
        elements.receiveInfo.textContent = error instanceof Error ? error.message : String(error);
        elements.receiveInfo.classList.remove('hidden');
      });
  });

  elements.downloadQrButton.addEventListener('click', () => {
    if (state) {
      downloadQrSvg(state, elements.qrContainer);
    }
  });

  const applyHashCredential = (): void => {
    const credential = readCredentialFromHash();
    if (!credential) {
      return;
    }
    applyReceiveCredential(credential, elements.receiveInfo);
  };

  window.addEventListener('hashchange', applyHashCredential);
  applyHashCredential();
}
