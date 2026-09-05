const { app, BrowserWindow, dialog, ipcMain, shell, nativeTheme } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const net = require('net');
const http = require('http');
const https = require('https');
const { TextDecoder } = require('util');
const { DESKTOP_BACKEND_DEFAULT_HOST, MAC_DESKTOP_CLI_PATH_ENTRIES, MAC_DESKTOP_SYSTEM_PATH_ENTRIES,
  DESKTOP_BACKEND_PATH_DELIMITER, PUBLIC_BIND_HOSTS, resolveAppDir, resolveBackendPath,
  resolveEnvExamplePath, resolvePackagedExeDir, isMac, isWindows, appRootDev, ensureDirectory,
  extendMacDesktopBackendPath } = require('./lib/app-paths');
const { initLogging, logLine } = require('./lib/logger');
const {
  DESKTOP_UPDATE_RUNTIME_RELATIVE_FILES, GITHUB_OWNER, GITHUB_REPO, RELEASES_PAGE_URL,
  LATEST_RELEASE_API_URL, DEFAULT_REQUEST_TIMEOUT_MS, DESKTOP_UPDATE_BACKUP_DIR,
  DESKTOP_UPDATE_BACKUP_MANIFEST_FILE, UPDATE_MODE, UPDATE_STATUS, backupPackagedRuntimeState,
  buildElectronUpdaterState, buildUpdateState, compareVersions, evaluateReleaseUpdate,
  extractReleaseMetadata, fetchLatestReleaseJson, isWindowsNsisInstalledApp,
  migrateMacPackagedRuntimeState, normalizeDownloadPercent, normalizeVersionString,
  parseSemver, resolveReleasePageUrlForVersion, resolveUpdaterLatestVersion,
  restorePackagedRuntimeStateFromBackup, sanitizeReleaseUrl, resolveUpdateBackupRoot,
  cleanupUpdateBackupRoot, resolveDesktopVersion,
} = require('./lib/update-core');

let mainWindow = null;
let backendProcess = null;
let logFilePath = null;
let backendStartError = null;
let desktopUpdateState = null;
let lastNotifiedUpdateVersion = '';
let lastPromptedInstallVersion = '';
let electronAutoUpdater = undefined;
let electronAutoUpdaterConfigured = false;
let electronUpdateCheckInFlight = false;
let desktopBackendOrigin = '';

function resolveWindowBackgroundColor() {
  return nativeTheme.shouldUseDarkColors ? '#08080c' : '#f4f7fb';
}

const DESKTOP_SHARE_IMAGE_WIDTH = 1080;
const DESKTOP_SHARE_IMAGE_INITIAL_HEIGHT = 720;
const DESKTOP_SHARE_IMAGE_MAX_HEIGHT = 20000;

async function checkForDesktopUpdates({
  currentVersion,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  fetchLatestRelease = fetchLatestReleaseJson,
} = {}) {
  const release = await fetchLatestRelease({ timeoutMs });
  return evaluateReleaseUpdate({ currentVersion, release });
}

desktopUpdateState = buildUpdateState();

function normalizeBackendHost(value, fallback = '') {
  const normalized = String(value || '').trim();
  return normalized || fallback;
}

function normalizeBackendBindHost(value, fallback = DESKTOP_BACKEND_DEFAULT_HOST) {
  const host = normalizeBackendHost(value, fallback);
  const lowerHost = host.toLowerCase();
  if (lowerHost === '*') {
    return '0.0.0.0';
  }
  if (lowerHost === '[::]') {
    return '::';
  }
  return host;
}

function hasOwnValue(object, key) {
  return Object.prototype.hasOwnProperty.call(object || {}, key);
}

function parseQuotedEnvValue(value, quote) {
  let result = '';
  for (let index = 1; index < value.length; index += 1) {
    const char = value[index];
    if (char === quote) {
      if (quote === '"') {
        return result.replace(/\\([nrt"\\$])/g, (_match, escaped) => {
          if (escaped === 'n') {
            return '\n';
          }
          if (escaped === 'r') {
            return '\r';
          }
          if (escaped === 't') {
            return '\t';
          }
          return escaped;
        });
      }
      return result.replace(/\\'/g, "'").replace(/\\\\/g, '\\');
    }
    result += char;
  }

  return value.trim();
}

function parseEnvScalarValue(rawValue) {
  const value = String(rawValue || '').trimStart();
  if (!value) {
    return '';
  }

  const quote = value[0];
  if (quote === '"' || quote === "'") {
    return parseQuotedEnvValue(value, quote);
  }

  for (let index = 0; index < value.length; index += 1) {
    if (value[index] === '#' && (index === 0 || /\s/.test(value[index - 1]))) {
      return value.slice(0, index).trim();
    }
  }

  return value.trim();
}

function expandEnvReferences(value, values = {}, sourceEnv = process.env) {
  return String(value || '').replace(
    /\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}/g,
    (_match, name, defaultValue) => {
      if (hasOwnValue(sourceEnv, name)) {
        return String(sourceEnv[name]);
      }
      if (hasOwnValue(values, name)) {
        return String(values[name]);
      }
      return defaultValue === undefined ? '' : defaultValue;
    }
  );
}

function readEnvFileValues(envFile, sourceEnv = process.env) {
  if (!envFile || !fs.existsSync(envFile)) {
    return {};
  }

  let content = '';
  try {
    content = fs.readFileSync(envFile, 'utf-8');
  } catch (_error) {
    return {};
  }

  const values = {};
  for (const line of content.split(/\r?\n/)) {
    const match = line.match(/^\uFEFF?\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!match) {
      continue;
    }
    values[match[1]] = expandEnvReferences(
      parseEnvScalarValue(match[2]),
      values,
      sourceEnv
    );
  }

  return values;
}

function readEnvFileValue(envFile, key, sourceEnv = process.env) {
  const values = readEnvFileValues(envFile, sourceEnv);
  return hasOwnValue(values, key) ? values[key] : null;
}

function resolveBackendBindHost({
  envFile,
  sourceEnv = process.env,
  fallback = DESKTOP_BACKEND_DEFAULT_HOST,
} = {}) {
  const sourceHost = normalizeBackendHost(sourceEnv.WEBUI_HOST);
  if (sourceHost) {
    return normalizeBackendBindHost(sourceHost, fallback);
  }

  const envFileHost = normalizeBackendHost(readEnvFileValue(envFile, 'WEBUI_HOST', sourceEnv));
  return normalizeBackendBindHost(envFileHost || fallback, fallback);
}

function resolveDesktopConnectHost(bindHost) {
  const host = normalizeBackendBindHost(bindHost, DESKTOP_BACKEND_DEFAULT_HOST);
  if (PUBLIC_BIND_HOSTS.has(host.toLowerCase())) {
    return DESKTOP_BACKEND_DEFAULT_HOST;
  }
  return host;
}

function formatUrlHost(host) {
  const normalized = normalizeBackendHost(host, DESKTOP_BACKEND_DEFAULT_HOST);
  if (normalized.startsWith('[') && normalized.endsWith(']')) {
    return normalized;
  }
  return normalized.includes(':') ? `[${normalized}]` : normalized;
}

function buildBackendUrl(host, port, pathname = '/') {
  const url = new URL(`http://${formatUrlHost(host)}:${port}/`);
  url.pathname = pathname;
  return url.toString();
}

function buildBackendArgs({ host, port }) {
  return [
    '--serve-only',
    '--host',
    normalizeBackendBindHost(host, DESKTOP_BACKEND_DEFAULT_HOST),
    '--port',
    String(port),
  ];
}

function buildBackendEnvironment({
  envFile,
  dbPath,
  logDir,
  port = null,
  host = null,
  sourceEnv = process.env,
}) {
  const selectedPort = Number(port);
  const selectedHost = normalizeBackendBindHost(
    normalizeBackendHost(host) || resolveBackendBindHost({ envFile, sourceEnv }),
    DESKTOP_BACKEND_DEFAULT_HOST
  );
  const env = {
    ...sourceEnv,
    DSA_DESKTOP_MODE: 'true',
    ENV_FILE: envFile,
    DATABASE_PATH: dbPath,
    LOG_DIR: logDir,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    WEBUI_HOST: selectedHost,
    WEBUI_ENABLED: 'false',
    BOT_ENABLED: 'false',
    DINGTALK_STREAM_ENABLED: 'false',
    FEISHU_STREAM_ENABLED: 'false',
  };

  if (Number.isInteger(selectedPort) && selectedPort >= 1 && selectedPort <= 65535) {
    env.WEBUI_PORT = String(selectedPort);
  }

  if (isMac) {
    env.PATH = extendMacDesktopBackendPath(sourceEnv.PATH);
  }

  return env;
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}


function decodeBackendOutput(data, decoder) {
  if (typeof data === 'string') {
    return data.trim();
  }
  if (!Buffer.isBuffer(data)) {
    return String(data).trim();
  }

  let decoded = decoder.decode(data, { stream: true });

  // Windows 控制台 / 子进程有时仍会吐出本地代码页字节，优先在明显乱码时回退到 GBK。
  if (isWindows && decoded.includes('\uFFFD')) {
    try {
      decoded = new TextDecoder('gbk', { fatal: false }).decode(data, { stream: true });
    } catch (_error) {
    }
  }

  return decoded.trim();
}

function formatCommand(command, args = []) {
  return [command, ...args]
    .map((part) => {
      const value = String(part);
      return value.includes(' ') ? `"${value}"` : value;
    })
    .join(' ');
}

function resolvePythonPath() {
  return process.env.DSA_PYTHON || 'python';
}

function ensureEnvFile(envPath) {
  if (fs.existsSync(envPath)) {
    return;
  }

  const envExample = resolveEnvExamplePath();
  if (fs.existsSync(envExample)) {
    fs.copyFileSync(envExample, envPath);
    return;
  }

  fs.writeFileSync(envPath, '# Configure your API keys and stock list here.\n', 'utf-8');
}

function findAvailablePort(startPort = 8000, endPort = 8100, host = DESKTOP_BACKEND_DEFAULT_HOST) {
  const bindHost = normalizeBackendBindHost(host, DESKTOP_BACKEND_DEFAULT_HOST);
  return new Promise((resolve, reject) => {
    const tryPort = (port) => {
      if (port > endPort) {
        reject(new Error('No available port'));
        return;
      }

      const server = net.createServer();
      server.once('error', () => {
        tryPort(port + 1);
      });
      server.once('listening', () => {
        server.close(() => resolve(port));
      });
      server.listen(port, bindHost);
    };

    tryPort(startPort);
  });
}

function waitForHealth(
  url,
  timeoutMs = 60000,
  intervalMs = 250,
  requestTimeoutMs = 1500,
  shouldAbort = null,
  onProgress = null
) {
  const start = Date.now();
  let attempts = 0;

  return new Promise((resolve, reject) => {
    let settled = false;
    let retryTimer = null;
    let activeRequest = null;

    const emitProgress = (payload) => {
      if (typeof onProgress !== 'function') {
        return;
      }
      try {
        onProgress(payload);
      } catch (_error) {
      }
    };

    const finish = (error, result) => {
      if (settled) {
        return;
      }
      settled = true;

      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }

      if (activeRequest && !activeRequest.destroyed) {
        activeRequest.destroy();
      }

      if (error) {
        emitProgress({
          type: 'final_error',
          elapsedMs: Date.now() - start,
          attempts,
          message: error.message,
        });
      }

      if (error) {
        reject(error);
      } else {
        resolve(result);
      }
    };

    const scheduleNext = () => {
      if (settled) {
        return;
      }
      retryTimer = setTimeout(attempt, intervalMs);
    };

    const attempt = () => {
      if (settled) {
        return;
      }

      if (typeof shouldAbort === 'function') {
        const abortReason = shouldAbort();
        if (abortReason) {
          emitProgress({
            type: 'aborted',
            elapsedMs: Date.now() - start,
            attempts,
            reason: abortReason,
          });
          finish(new Error(`Health check aborted: ${abortReason}`));
          return;
        }
      }

      const elapsedMs = Date.now() - start;
      if (elapsedMs > timeoutMs) {
        emitProgress({
          type: 'total_timeout',
          elapsedMs,
          attempts,
          timeoutMs,
        });
        finish(new Error(`Health check timeout after ${elapsedMs}ms`));
        return;
      }

      attempts += 1;
      emitProgress({
        type: 'probe_start',
        elapsedMs,
        attempts,
      });

      activeRequest = http.get(url, (res) => {
        if (settled) {
          return;
        }

        res.resume();
        if (res.statusCode === 200) {
          const readyElapsedMs = Date.now() - start;
          emitProgress({
            type: 'ready',
            elapsedMs: readyElapsedMs,
            attempts,
          });
          finish(null, { elapsedMs: readyElapsedMs, attempts });
          return;
        }

        emitProgress({
          type: 'probe_status',
          elapsedMs: Date.now() - start,
          attempts,
          statusCode: res.statusCode,
        });
        scheduleNext();
      });

      activeRequest.setTimeout(requestTimeoutMs, () => {
        emitProgress({
          type: 'probe_timeout',
          elapsedMs: Date.now() - start,
          attempts,
          requestTimeoutMs,
        });
        activeRequest.destroy(new Error(`Health probe request timeout after ${requestTimeoutMs}ms`));
      });

      activeRequest.on('error', (error) => {
        if (settled) {
          return;
        }

        emitProgress({
          type: 'probe_error',
          elapsedMs: Date.now() - start,
          attempts,
          errorCode: error.code || 'unknown',
          errorMessage: error.message,
        });
        scheduleNext();
      });
    };

    attempt();
  });
}

function startBackend({ port, envFile, dbPath, logDir, host = null }) {
  const backendPath = resolveBackendPath();
  backendStartError = null;
  const launchStartedAt = Date.now();
  const bindHost = normalizeBackendBindHost(
    normalizeBackendHost(host) || resolveBackendBindHost({ envFile }),
    DESKTOP_BACKEND_DEFAULT_HOST
  );

  const env = buildBackendEnvironment({ envFile, dbPath, logDir, port, host: bindHost });

  const args = buildBackendArgs({ host: bindHost, port });
  let launchMode = '';
  let launchCommand = '';
  let launchCwd = '';

  if (backendPath) {
    if (!fs.existsSync(backendPath)) {
      throw new Error(`Backend executable not found: ${backendPath}`);
    }
    launchMode = 'packaged';
    launchCommand = formatCommand(backendPath, args);
    launchCwd = path.dirname(backendPath);
    backendProcess = spawn(backendPath, args, {
      env,
      cwd: launchCwd,
      stdio: 'pipe',
      windowsHide: true,
    });
  } else {
    const pythonPath = resolvePythonPath();
    const scriptPath = path.join(appRootDev, 'main.py');
    const pythonArgs = ['-X', 'utf8', scriptPath, ...args];
    launchMode = 'development';
    launchCommand = formatCommand(pythonPath, pythonArgs);
    launchCwd = appRootDev;
    backendProcess = spawn(pythonPath, pythonArgs, {
      env,
      cwd: launchCwd,
      stdio: 'pipe',
      windowsHide: true,
    });
  }

  if (backendProcess) {
    let firstStdoutLogged = false;
    let firstStderrLogged = false;
    const stdoutDecoder = new TextDecoder('utf-8', { fatal: false });
    const stderrDecoder = new TextDecoder('utf-8', { fatal: false });

    backendProcess.once('spawn', () => {
      logLine(`[backend] spawned pid=${backendProcess.pid} in ${Date.now() - launchStartedAt}ms`);
    });
    backendProcess.on('error', (error) => {
      backendStartError = error;
      logLine(`[backend] failed to start: ${error.message}`);
    });
    backendProcess.stdout.on('data', (data) => {
      if (!firstStdoutLogged) {
        firstStdoutLogged = true;
        logLine(`[backend] first stdout after ${Date.now() - launchStartedAt}ms`);
      }
      logLine(`[backend] ${decodeBackendOutput(data, stdoutDecoder)}`);
    });
    backendProcess.stderr.on('data', (data) => {
      if (!firstStderrLogged) {
        firstStderrLogged = true;
        logLine(`[backend] first stderr after ${Date.now() - launchStartedAt}ms`);
      }
      logLine(`[backend] ${decodeBackendOutput(data, stderrDecoder)}`);
    });
    backendProcess.on('exit', (code, signal) => {
      logLine(`[backend] exited with code ${code}, signal ${signal || 'none'}`);
    });
  }

  return {
    mode: launchMode,
    command: launchCommand,
    cwd: launchCwd,
  };
}

function waitForBackendExit(processRef, timeoutMs = 5000) {
  if (!processRef || processRef.exitCode !== null || processRef.signalCode) {
    return Promise.resolve(true);
  }

  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    let onExit = null;

    const done = (exited) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      if (onExit) {
        processRef.removeListener('exit', onExit);
      }
      resolve(exited || processRef.exitCode !== null || Boolean(processRef.signalCode));
    };

    onExit = () => done(true);

    timer = setTimeout(() => {
      done(false);
    }, timeoutMs);

    processRef.once('exit', onExit);
  });
}

function __setBackendProcessForTest(processRef = null) {
  backendProcess = processRef;
}

function clearBackendProcessIfCurrent(processRef) {
  if (backendProcess === processRef) {
    backendProcess = null;
  }
}

function stopBackend() {
  if (!backendProcess) {
    return Promise.resolve();
  }
  const processToStop = backendProcess;
  if (processToStop.exitCode !== null || processToStop.signalCode) {
    clearBackendProcessIfCurrent(processToStop);
    return Promise.resolve();
  }

  const waitAndClear = () => waitForBackendExit(processToStop, 10000)
    .then((exited) => {
      if (!exited) {
        return;
      }
      clearBackendProcessIfCurrent(processToStop);
    });

  if (isWindows) {
    spawn('taskkill', ['/PID', String(processToStop.pid), '/T', '/F'], { windowsHide: true }).on('error', () => {
    });
    return waitAndClear();
  }

  if (!processToStop.killed) {
    processToStop.kill('SIGTERM');
  }
  setTimeout(() => {
    if (processToStop.killed || processToStop.exitCode !== null || processToStop.signalCode) {
      return;
    }
    try {
      processToStop.kill('SIGKILL');
    } catch (_error) {
    }
  }, 3000);

  return waitAndClear();
}

function buildMainPageUrl(port, timestamp = Date.now(), host = DESKTOP_BACKEND_DEFAULT_HOST) {
  const url = new URL(buildBackendUrl(host, port, '/'));
  url.searchParams.set('desktop_version', resolveDesktopVersion() || 'unknown');
  url.searchParams.set('cache_bust', String(timestamp));
  return url.toString();
}

function buildDesktopShareImageUrl(pageUrl, recordId, expectedBackendOrigin = '') {
  if (!Number.isSafeInteger(recordId) || recordId <= 0) {
    throw new Error('Invalid share image record ID');
  }

  let page;
  try {
    page = new URL(pageUrl);
  } catch (_error) {
    throw new Error('Desktop backend URL is unavailable');
  }

  let expectedOrigin = page.origin;
  if (expectedBackendOrigin) {
    try {
      expectedOrigin = new URL(expectedBackendOrigin).origin;
    } catch (_error) {
      throw new Error('Desktop backend origin is invalid');
    }
  }
  if (page.protocol !== 'http:' || !page.port || page.origin !== expectedOrigin) {
    throw new Error('Desktop share images require the configured backend origin');
  }

  return new URL(
    `/api/v1/history/${recordId}/share-image-html`,
    page.origin
  ).toString();
}

async function renderDesktopShareImage(
  recordId,
  {
    sourceWindow = mainWindow,
    BrowserWindowClass = BrowserWindow,
    backendOrigin = '',
  } = {}
) {
  if (!sourceWindow || sourceWindow.isDestroyed() || !sourceWindow.webContents) {
    throw new Error('Desktop window is unavailable');
  }

  const targetUrl = buildDesktopShareImageUrl(
    sourceWindow.webContents.getURL(),
    recordId,
    backendOrigin
  );
  let renderWindow = null;
  try {
    renderWindow = new BrowserWindowClass({
      show: false,
      width: DESKTOP_SHARE_IMAGE_WIDTH,
      height: DESKTOP_SHARE_IMAGE_INITIAL_HEIGHT,
      ...(isMac ? { enableLargerThanScreen: true } : {}),
      useContentSize: true,
      backgroundColor: '#eef4fd',
      webPreferences: {
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
        backgroundThrottling: false,
      },
    });
    renderWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    renderWindow.webContents.on('will-navigate', (event, navigationUrl) => {
      if (navigationUrl !== targetUrl) {
        event.preventDefault();
      }
    });

    await renderWindow.loadURL(targetUrl);
    const pageMetrics = await renderWindow.webContents.executeJavaScript(`({
      contentType: document.contentType,
      width: Math.ceil(Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)),
      height: Math.ceil(Math.max(document.documentElement.scrollHeight, document.body.scrollHeight))
    })`);
    if (!pageMetrics || pageMetrics.contentType !== 'text/html') {
      throw new Error('Desktop share image source did not return HTML');
    }
    if (
      !Number.isFinite(pageMetrics.width)
      || pageMetrics.width !== DESKTOP_SHARE_IMAGE_WIDTH
      || !Number.isFinite(pageMetrics.height)
      || pageMetrics.height < 1
      || pageMetrics.height > DESKTOP_SHARE_IMAGE_MAX_HEIGHT
    ) {
      throw new Error(`Desktop share image has invalid dimensions: ${pageMetrics.width}x${pageMetrics.height}`);
    }

    renderWindow.setContentSize(DESKTOP_SHARE_IMAGE_WIDTH, pageMetrics.height);
    await renderWindow.webContents.executeJavaScript(
      'new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))'
    );
    const image = await renderWindow.webContents.capturePage({
      x: 0,
      y: 0,
      width: DESKTOP_SHARE_IMAGE_WIDTH,
      height: pageMetrics.height,
    });
    if (!image || image.isEmpty()) {
      throw new Error('Desktop share image capture returned an empty image');
    }

    const png = image.toPNG();
    return png.buffer.slice(png.byteOffset, png.byteOffset + png.byteLength);
  } finally {
    if (renderWindow && !renderWindow.isDestroyed()) {
      renderWindow.destroy();
    }
  }
}

function getElectronAutoUpdater() {
  if (electronAutoUpdater !== undefined) {
    return electronAutoUpdater;
  }

  if (!isWindowsNsisInstalledApp()) {
    electronAutoUpdater = null;
    return electronAutoUpdater;
  }

  try {
    electronAutoUpdater = require('electron-updater').autoUpdater;
  } catch (error) {
    electronAutoUpdater = null;
    logLine(`[update] electron-updater unavailable: ${error instanceof Error ? error.message : String(error)}`);
  }

  return electronAutoUpdater;
}

function canUseElectronAutoUpdater() {
  return Boolean(getElectronAutoUpdater());
}

function broadcastDesktopUpdateState() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  mainWindow.webContents.send('desktop:update-state', desktopUpdateState);
}

function setDesktopUpdateState(nextState) {
  desktopUpdateState = buildUpdateState({
    currentVersion: resolveDesktopVersion(),
    ...nextState,
  });
  broadcastDesktopUpdateState();
  return desktopUpdateState;
}

async function maybePromptDesktopUpdate(state) {
  if (!state || state.status !== UPDATE_STATUS.UPDATE_AVAILABLE) {
    return;
  }
  if (state.updateMode === UPDATE_MODE.AUTO) {
    return;
  }
  if (!state.latestVersion || state.latestVersion === lastNotifiedUpdateVersion) {
    return;
  }
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }

  lastNotifiedUpdateVersion = state.latestVersion;
  const currentVersion = state.currentVersion || resolveDesktopVersion() || '当前版本';
  const result = await dialog.showMessageBox(mainWindow, {
    type: 'info',
    buttons: ['稍后', '前往下载'],
    defaultId: 1,
    cancelId: 0,
    title: '发现新版本',
    message: `检测到桌面端新版本 ${state.latestVersion}`,
    detail: `当前版本 ${currentVersion}。新版本将跳转到 GitHub Releases 下载页，不会静默下载或自动安装。`,
    noLink: true,
  });

  if (result.response === 1) {
    await shell.openExternal(sanitizeReleaseUrl(state.releaseUrl));
  }
}

async function installDownloadedUpdate() {
  const updater = getElectronAutoUpdater();
  if (!updater) {
    throw new Error('当前运行模式不支持自动安装更新。');
  }
  if (desktopUpdateState?.status !== UPDATE_STATUS.UPDATE_DOWNLOADED) {
    throw new Error('更新尚未下载完成，无法自动安装。');
  }

  setDesktopUpdateState({
    status: UPDATE_STATUS.INSTALLING,
    updateMode: UPDATE_MODE.AUTO,
    latestVersion: desktopUpdateState?.latestVersion || '',
    releaseUrl: desktopUpdateState?.releaseUrl || RELEASES_PAGE_URL,
    message: '正在重启并安装更新...',
  });
  let backupRoot = null;
  try {
    logLine('[update] stop backend and backup runtime data before install');
    await stopBackend();
    backupRoot = resolveUpdateBackupRoot();
    cleanupUpdateBackupRoot();

    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        backupPackagedRuntimeState();
        break;
      } catch (error) {
        if (attempt === 3) {
          setDesktopUpdateState({
            status: UPDATE_STATUS.ERROR,
            updateMode: UPDATE_MODE.AUTO,
            currentVersion: resolveDesktopVersion(),
            latestVersion: desktopUpdateState?.latestVersion || '',
            releaseUrl: desktopUpdateState?.releaseUrl || RELEASES_PAGE_URL,
            checkedAt: new Date().toISOString(),
            message: `更新安装准备失败：${error instanceof Error ? error.message : String(error)}`,
          });
          throw error;
        }

        await sleep(300 * attempt);
      }
    }

    logLine('[update] silent quit and install requested');
    updater.quitAndInstall(true, true);
    return true;
  } catch (error) {
    if (backupRoot) {
      cleanupUpdateBackupRoot();
    }
    logLine(`[update] install downloaded update failed: ${error instanceof Error ? error.message : String(error)}`);
    throw error;
  }
}

async function maybePromptInstallDownloadedUpdate(state) {
  if (!state || state.status !== UPDATE_STATUS.UPDATE_DOWNLOADED || state.updateMode !== UPDATE_MODE.AUTO) {
    return;
  }
  if (!state.latestVersion || state.latestVersion === lastPromptedInstallVersion) {
    return;
  }
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }

  lastPromptedInstallVersion = state.latestVersion;
  const result = await dialog.showMessageBox(mainWindow, {
    type: 'info',
    buttons: ['稍后', '立即重启安装'],
    defaultId: 1,
    cancelId: 0,
    title: '更新已下载',
    message: `桌面端新版本 ${state.latestVersion} 已下载`,
    detail: '重启应用后会自动完成安装。未保存的设置草稿请先保存。',
    noLink: true,
  });

  if (result.response === 1) {
    try {
      await installDownloadedUpdate();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      logLine(`[update] auto install prompt failed: ${message}`);
      setDesktopUpdateState({
        status: UPDATE_STATUS.ERROR,
        updateMode: UPDATE_MODE.AUTO,
        currentVersion: resolveDesktopVersion(),
        latestVersion: state.latestVersion || desktopUpdateState?.latestVersion || '',
        releaseUrl: state.releaseUrl || desktopUpdateState?.releaseUrl || RELEASES_PAGE_URL,
        checkedAt: new Date().toISOString(),
        message: `更新安装失败：${message}。可先保存草稿并前往下载页，或稍后重试。`,
      });
    }
  }
}

function configureElectronAutoUpdater() {
  const updater = getElectronAutoUpdater();
  if (!updater || electronAutoUpdaterConfigured) {
    return updater;
  }

  updater.autoDownload = true;
  updater.autoInstallOnAppQuit = false;
  if (isWindows && app.isPackaged) {
    const installDirectory = path.dirname(app.getPath('exe'));
    if (installDirectory) {
      updater.installDirectory = installDirectory;
      logLine(`[update] auto updater install directory set to ${updater.installDirectory}`);
    }
  }

  updater.on('checking-for-update', () => {
    setDesktopUpdateState({
      status: UPDATE_STATUS.CHECKING,
      updateMode: UPDATE_MODE.AUTO,
      currentVersion: resolveDesktopVersion(),
      message: '正在检查桌面端更新...',
    });
  });

  updater.on('update-available', (info = {}) => {
    const latestVersion = resolveUpdaterLatestVersion(info) || '最新版本';
    const nextState = buildElectronUpdaterState(UPDATE_STATUS.UPDATE_AVAILABLE, info, {
      message: `发现新版本 ${latestVersion}，正在后台下载更新...`,
    });
    setDesktopUpdateState(nextState);
    logLine(`[update] auto update available latest=${nextState.latestVersion || 'unknown'}`);
  });

  updater.on('update-not-available', (info = {}) => {
    const nextState = buildElectronUpdaterState(UPDATE_STATUS.UP_TO_DATE, info, {
      message: '当前桌面端已是最新版本。',
    });
    setDesktopUpdateState(nextState);
    logLine(`[update] auto update not available current=${nextState.currentVersion || 'unknown'}`);
  });

  updater.on('download-progress', (progress = {}) => {
    const percent = normalizeDownloadPercent(progress.percent);
    const nextState = setDesktopUpdateState({
      status: UPDATE_STATUS.DOWNLOADING,
      updateMode: UPDATE_MODE.AUTO,
      latestVersion: desktopUpdateState?.latestVersion || '',
      releaseUrl: desktopUpdateState?.releaseUrl || RELEASES_PAGE_URL,
      downloadPercent: percent,
      downloadedBytes: progress.transferred,
      totalBytes: progress.total,
      message:
        percent === null
          ? '正在下载桌面端更新...'
          : `正在下载桌面端更新（${percent.toFixed(percent % 1 === 0 ? 0 : 1)}%）...`,
    });
    logLine(`[update] download progress percent=${nextState.downloadPercent ?? 'unknown'}`);
  });

  updater.on('update-downloaded', (info = {}) => {
    const latestVersion = resolveUpdaterLatestVersion(info) || desktopUpdateState?.latestVersion || '';
    const nextState = buildElectronUpdaterState(UPDATE_STATUS.UPDATE_DOWNLOADED, info, {
      latestVersion,
      downloadPercent: 100,
      message: latestVersion
        ? `新版本 ${latestVersion} 已下载，可重启应用完成安装。`
        : '新版本已下载，可重启应用完成安装。',
    });
    setDesktopUpdateState(nextState);
    logLine(`[update] downloaded latest=${nextState.latestVersion || 'unknown'}`);
    void maybePromptInstallDownloadedUpdate(nextState);
  });

  updater.on('error', (error) => {
    const message = error instanceof Error ? error.message : String(error);
    logLine(`[update] auto updater failed: ${message}`);
    setDesktopUpdateState({
      status: UPDATE_STATUS.ERROR,
      updateMode: UPDATE_MODE.AUTO,
      currentVersion: resolveDesktopVersion(),
      latestVersion: desktopUpdateState?.latestVersion || '',
      releaseUrl: desktopUpdateState?.releaseUrl || RELEASES_PAGE_URL,
      checkedAt: new Date().toISOString(),
      message: `自动更新失败：${message}`,
    });
  });

  electronAutoUpdaterConfigured = true;
  return updater;
}

async function performElectronUpdaterCheck({ manual = false } = {}) {
  const updater = configureElectronAutoUpdater();
  if (!updater) {
    throw new Error('当前平台不支持自动安装更新。');
  }
  if (electronUpdateCheckInFlight) {
    return desktopUpdateState;
  }

  electronUpdateCheckInFlight = true;
  setDesktopUpdateState({
    status: UPDATE_STATUS.CHECKING,
    updateMode: UPDATE_MODE.AUTO,
    currentVersion: resolveDesktopVersion(),
    message: manual ? '正在检查桌面端更新...' : '正在后台检查桌面端更新...',
  });

  try {
    await updater.checkForUpdates();
    return desktopUpdateState;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    logLine(`[update] auto updater check failed: ${message}`);
    const nextState = setDesktopUpdateState({
      status: manual ? UPDATE_STATUS.ERROR : UPDATE_STATUS.IDLE,
      updateMode: UPDATE_MODE.AUTO,
      currentVersion: resolveDesktopVersion(),
      checkedAt: new Date().toISOString(),
      message: manual ? `检查更新失败：${message}` : '',
    });
    return nextState;
  } finally {
    electronUpdateCheckInFlight = false;
  }
}

async function performDesktopUpdateCheck({ manual = false, notify = false } = {}) {
  if (canUseElectronAutoUpdater()) {
    return performElectronUpdaterCheck({ manual, notify });
  }

  const currentVersion = resolveDesktopVersion();
  setDesktopUpdateState({
    status: UPDATE_STATUS.CHECKING,
    currentVersion,
    message: manual ? '正在检查桌面端更新...' : '正在后台检查桌面端更新...',
  });

  try {
    const nextState = await checkForDesktopUpdates({ currentVersion });
    const resolvedState = setDesktopUpdateState(nextState);
    logLine(
      `[update] status=${resolvedState.status} current=${resolvedState.currentVersion || 'unknown'} latest=${resolvedState.latestVersion || 'unknown'}`
    );
    if (notify) {
      await maybePromptDesktopUpdate(resolvedState);
    }
    return resolvedState;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    logLine(`[update] check failed: ${message}`);

    if (manual) {
      return setDesktopUpdateState({
        status: UPDATE_STATUS.ERROR,
        currentVersion,
        checkedAt: new Date().toISOString(),
        message: `检查更新失败：${message}`,
      });
    }

    return setDesktopUpdateState({
      status: UPDATE_STATUS.IDLE,
      currentVersion,
      checkedAt: new Date().toISOString(),
      message: '',
    });
  }
}

ipcMain.handle('desktop:get-update-state', () => desktopUpdateState);
ipcMain.handle('desktop:check-for-updates', () => performDesktopUpdateCheck({ manual: true }));
ipcMain.handle('desktop:install-downloaded-update', () => installDownloadedUpdate());
ipcMain.handle('desktop:open-release-page', async (_event, releaseUrl) => {
  await shell.openExternal(sanitizeReleaseUrl(releaseUrl));
  return true;
});
ipcMain.handle('desktop:render-share-image', async (event, recordId) => {
  if (!mainWindow || mainWindow.isDestroyed() || event.sender !== mainWindow.webContents) {
    throw new Error('Share image request did not originate from the desktop window');
  }
  try {
    return await renderDesktopShareImage(recordId, { backendOrigin: desktopBackendOrigin });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    logLine(`[share-image] desktop render failed for record=${recordId}: ${message}`);
    throw error;
  }
});

async function createWindow() {
  desktopBackendOrigin = '';
  const restoreResult = isWindowsNsisInstalledApp() ? restorePackagedRuntimeStateFromBackup() : null;
  const macMigrationResult = migrateMacPackagedRuntimeState();
  initLogging();
  if (macMigrationResult.migrated.length) {
    logLine(`[migration] migrated macOS runtime files from ${macMigrationResult.sourceDir} to ${macMigrationResult.targetDir}: ${macMigrationResult.migrated.join(', ')}`);
  }
  if (macMigrationResult.skipped.length) {
    logLine(`[migration] skipped existing macOS runtime files: ${macMigrationResult.skipped.join(', ')}`);
  }
  if (macMigrationResult.failed.length) {
    logLine(`[migration] failed to migrate macOS runtime files: ${macMigrationResult.failed.join(', ')}`);
  }
  const restoreFailed = Boolean(restoreResult && restoreResult.failed.length);
  const restoreIssueDetails = restoreResult
    ? restoreResult.failed.join('；')
    : '';
  const restoreErrorMessage = restoreFailed
    ? `上次更新安装未完成或恢复运行时文件失败，已保留备份目录 ${restoreResult.backupRoot}，请确认后手动恢复并重启应用。明细：${restoreIssueDetails}`
    : '';
  setDesktopUpdateState({
    status: restoreFailed ? UPDATE_STATUS.ERROR : UPDATE_STATUS.IDLE,
    currentVersion: resolveDesktopVersion(),
    updateMode: restoreFailed ? UPDATE_MODE.MANUAL : UPDATE_MODE.AUTO,
    message: restoreErrorMessage,
  });
  const startupStartedAt = Date.now();
  const logStartup = (message) => {
    logLine(`[startup +${Date.now() - startupStartedAt}ms] ${message}`);
  };

  logStartup('createWindow started');

  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 960,
    minHeight: 640,
    backgroundColor: resolveWindowBackgroundColor(),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
      additionalArguments: [`--dsa-desktop-version=${app.getVersion()}`],
    },
  });
  logStartup('BrowserWindow created');

  const loadingPath = path.join(__dirname, 'renderer', 'loading.html');
  const loadingPageStartedAt = Date.now();
  await mainWindow.loadFile(loadingPath);
  logStartup(`Loading page rendered in ${Date.now() - loadingPageStartedAt}ms`);

  const applyThemeBackground = () => {
    if (!mainWindow || mainWindow.isDestroyed()) {
      return;
    }
    mainWindow.setBackgroundColor(resolveWindowBackgroundColor());
  };
  nativeTheme.on('updated', applyThemeBackground);
  mainWindow.once('closed', () => {
    nativeTheme.removeListener('updated', applyThemeBackground);
  });

  const webViewStartedAt = Date.now();
  mainWindow.webContents.on('did-start-loading', () => {
    logStartup('WebContents did-start-loading');
  });
  mainWindow.webContents.on('dom-ready', () => {
    logStartup(`WebContents dom-ready (+${Date.now() - webViewStartedAt}ms after events attached)`);
  });
  mainWindow.webContents.on('did-finish-load', () => {
    logStartup(`WebContents did-finish-load (+${Date.now() - webViewStartedAt}ms after events attached)`);
  });
  mainWindow.webContents.on(
    'did-fail-load',
    (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
      logStartup(
        `WebContents did-fail-load code=${errorCode} mainFrame=${isMainFrame} url=${validatedURL} reason=${errorDescription}`
      );
    }
  );

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  const appDir = resolveAppDir();
  const envPath = path.join(appDir, '.env');
  ensureEnvFile(envPath);
  logStartup(`Env file ready: ${envPath}`);

  const backendBindHost = resolveBackendBindHost({ envFile: envPath });
  const backendConnectHost = resolveDesktopConnectHost(backendBindHost);
  logStartup(`Backend bind host=${backendBindHost}; desktop connect host=${backendConnectHost}`);

  const portFindStartedAt = Date.now();
  const port = await findAvailablePort(8000, 8100, backendBindHost);
  logStartup(`Using port ${port} (selected in ${Date.now() - portFindStartedAt}ms)`);
  desktopBackendOrigin = new URL(buildBackendUrl(backendConnectHost, port)).origin;
  logStartup(`App directory=${appDir}`);

  const dbPath = path.join(appDir, 'data', 'stock_analysis.db');
  const logDir = path.join(appDir, 'logs');

  try {
    const launchInfo = startBackend({ port, envFile: envPath, dbPath, logDir, host: backendBindHost });
    logStartup(`Backend launch mode=${launchInfo.mode}`);
    logStartup(`Backend launch command=${launchInfo.command}`);
    logStartup(`Backend launch cwd=${launchInfo.cwd}`);
    logStartup('Waiting for backend health check');
  } catch (error) {
    logStartup(`Backend launch failed: ${String(error)}`);
    const errorUrl = `file://${loadingPath}?error=${encodeURIComponent(String(error))}`;
    await mainWindow.loadURL(errorUrl);
    return;
  }

  const healthUrl = buildBackendUrl(backendConnectHost, port, '/api/health');
  let lastHealthProgressLogAt = 0;
  const healthProgressLogIntervalMs = 2000;

  const onHealthProgress = (event) => {
    if (!event || event.type === 'probe_start') {
      return;
    }

    if (event.type === 'ready') {
      logStartup(`Health ready in ${event.elapsedMs}ms (attempts=${event.attempts})`);
      return;
    }

    if (event.type === 'aborted' || event.type === 'total_timeout' || event.type === 'final_error') {
      const details = event.reason || event.message || '';
      logStartup(`Health ${event.type} after ${event.elapsedMs}ms (attempts=${event.attempts}) ${details}`.trim());
      return;
    }

    const now = Date.now();
    if (now - lastHealthProgressLogAt < healthProgressLogIntervalMs) {
      return;
    }

    lastHealthProgressLogAt = now;
    let detail = '';
    if (event.type === 'probe_status') {
      detail = `status=${event.statusCode}`;
    } else if (event.type === 'probe_timeout') {
      detail = `probeTimeout=${event.requestTimeoutMs}ms`;
    } else if (event.type === 'probe_error') {
      detail = `error=${event.errorCode}:${event.errorMessage}`;
    }

    logStartup(
      `Waiting for backend health... elapsed=${event.elapsedMs}ms attempts=${event.attempts}${detail ? ` ${detail}` : ''}`
    );
  };

  try {
    const healthInfo = await waitForHealth(
      healthUrl,
      60000,
      250,
      1500,
      () => {
        if (backendStartError) {
          return `backend start error: ${backendStartError.message}`;
        }
        if (!backendProcess) {
          return 'backend process is unavailable';
        }
        if (backendProcess.exitCode !== null) {
          return `backend exited with code ${backendProcess.exitCode}`;
        }
        if (backendProcess.signalCode) {
          return `backend exited by signal ${backendProcess.signalCode}`;
        }
        return null;
      },
      onHealthProgress
    );
    logStartup(`Backend ready in ${healthInfo.elapsedMs}ms (${healthInfo.attempts} probes)`);
    const mainPageStartedAt = Date.now();
    const mainPageUrl = buildMainPageUrl(port, Date.now(), backendConnectHost);
    await mainWindow.loadURL(mainPageUrl);
    logStartup(`Main page loadURL resolved in ${Date.now() - mainPageStartedAt}ms url=${mainPageUrl}`);
    logStartup(`Main UI loaded in ${Date.now() - startupStartedAt}ms`);
    if (!restoreFailed) {
      void performDesktopUpdateCheck({ notify: true });
    }
  } catch (error) {
    logStartup(`Startup failed while waiting for health: ${String(error)}`);
    const errorUrl = `file://${loadingPath}?error=${encodeURIComponent(String(error))}`;
    await mainWindow.loadURL(errorUrl);
  }
}

app.whenReady().then(createWindow);

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});

app.on('window-all-closed', () => {
  void stopBackend();
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  void stopBackend();
});

module.exports = {
  DEFAULT_REQUEST_TIMEOUT_MS,
  GITHUB_OWNER,
  GITHUB_REPO,
  LATEST_RELEASE_API_URL,
  RELEASES_PAGE_URL,
  DESKTOP_UPDATE_RUNTIME_RELATIVE_FILES,
  UPDATE_MODE,
  UPDATE_STATUS,
  buildUpdateState,
  backupPackagedRuntimeState,
  buildBackendArgs,
  checkForDesktopUpdates,
  compareVersions,
  evaluateReleaseUpdate,
  buildBackendUrl,
  buildBackendEnvironment,
  extendMacDesktopBackendPath,
  extractReleaseMetadata,
  fetchLatestReleaseJson,
  findAvailablePort,
  buildMainPageUrl,
  buildDesktopShareImageUrl,
  migrateMacPackagedRuntimeState,
  normalizeVersionString,
  parseSemver,
  readEnvFileValue,
  resolveAppDir,
  resolveBackendBindHost,
  resolveDesktopConnectHost,
  renderDesktopShareImage,
  restorePackagedRuntimeStateFromBackup,
  sanitizeReleaseUrl,
  startBackend,
  stopBackend,
  __getBackendProcessForTest() {
    return backendProcess;
  },
  __setBackendProcessForTest,
  __setMainWindowForTest(mainWindowRef = null) {
    mainWindow = mainWindowRef;
  },
  waitForBackendExit,
};
