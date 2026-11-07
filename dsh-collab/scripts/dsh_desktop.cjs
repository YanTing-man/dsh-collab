#!/usr/bin/env node
/*
 * dsh_desktop.cjs - Desktop transport helper for the dsh-collab bridge.
 *
 * Run it with the Desktop's own Electron binary in Node mode
 * (`ELECTRON_RUN_AS_NODE=1 "<DeepSeek Harness.exe>" dsh_desktop.cjs ...`), the
 * same interpreter the installed Desktop plugin itself runs under.  It speaks
 * the Host's native, fully authenticated transport:
 *
 *   1. read the private launch descriptor the user-installed Desktop plugin
 *      wrote (protocol/backend/pid/executable/authenticatedUrl),
 *   2. exchange the Host's normal process launch token for its normal
 *      browser-session cookie (a plain `GET /?token=...` answering 303 with
 *      Set-Cookie, exactly what the Desktop window itself does),
 *   3. call the ordinary `/api/<namespace>/<method>` Remote endpoints and open
 *      the ordinary `/api/remote.mux` WebSocket with that cookie.
 *
 * Nothing here disables, weakens, or forges authentication: no other user's
 * cookie is read, the Host keeps its permission model, no extra route is
 * registered, and redirects are never followed (this client has no redirect
 * logic at all), so the token and the cookie can only ever be presented to the
 * descriptor's own loopback origin.  The token, the cookie and raw request
 * headers are never printed or logged.
 *
 * stdout carries newline-delimited JSON events for the Python parent; stderr
 * carries human diagnostics only.
 *
 * Usage:  node dsh_desktop.cjs <endpoint.json> <doctor|models|prepare|select-model|turn>
 *         The command's parameters arrive as ONE JSON line on stdin.
 *         A subsequent `{"cmd":"cancel"}` line asks for session/cancel.
 */

'use strict';

const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { createRequire } = require('node:module');
const { randomUUID } = require('node:crypto');

const EXIT_OK = 0;
const EXIT_USAGE = 2;
const EXIT_CONNECT = 3;
const EXIT_TURN = 4;
const EXIT_CANCELLED = 5;

const RPC_TIMEOUT_MS = 60000;
const CONNECT_TIMEOUT_MS = 15000;
const MAX_RESPONSE_BYTES = 128 * 1024 * 1024;
const DEFAULT_TIME_ZONE = 'Asia/Shanghai';

class UsageError extends Error {}
class ConnectError extends Error {}
class RemoteFailure extends Error {
  constructor(message, code, details) {
    super(message);
    this.code = code;
    this.details = details;
  }
}

const transportSecrets = new Set();
function safeText(value) {
  let text = String(value);
  for (const secret of transportSecrets) if (secret) text = text.split(secret).join('[redacted]');
  return text.replace(/([?&]token=)[^\s&"'<>]+/gi, '$1[redacted]')
    .replace(/(Bearer\s+)[A-Za-z0-9._~+\/-]+/gi, '$1[redacted]');
}
function safeValue(value) {
  if (typeof value === 'string') return safeText(value);
  if (Array.isArray(value)) return value.map(safeValue);
  if (!value || typeof value !== 'object') return value;
  if (['thinking', 'reasoning', 'analysis'].includes(value.type)) return { type: value.type, _redacted: true };
  const result = {};
  for (const [key, part] of Object.entries(value)) {
    if (/^(?:authorization|cookie|set-cookie|authenticatedUrl|api[_-]?key|password|secret|access[_-]?token|refresh[_-]?token|thinking)$/i.test(key)
        || key === 'reasoning' && !(part && typeof part === 'object' && Array.isArray(part.efforts))) {
      result[key] = '[redacted]';
    } else result[key] = safeValue(part);
  }
  return result;
}
function emit(event) {
  process.stdout.write(JSON.stringify(safeValue(event)) + '\n');
}

function diag(message) {
  process.stderr.write(safeText(message) + '\n');
}

function errorText(error) {
  if (error === undefined || error === null) return 'unknown error';
  if (error instanceof Error) return error.message;
  return String(error);
}

function finish(code) {
  process.exitCode = code;
  try {
    process.stdout.write('', () => process.exit(code));
  } catch {
    process.exit(code);
  }
}

// ---------------------------------------------------------------------------
// Endpoint descriptor (never echoed back: it carries the launch token)
// ---------------------------------------------------------------------------

function isLoopbackName(name) {
  return name === '127.0.0.1' || name === '::1' || name === 'localhost';
}

function readEndpoint(file) {
  let raw;
  try {
    raw = fs.readFileSync(file, 'utf8');
  } catch (error) {
    throw new ConnectError(`cannot read the Desktop endpoint descriptor ${file}: ${errorText(error)}`);
  }
  let descriptor;
  try {
    descriptor = JSON.parse(raw);
  } catch (error) {
    throw new ConnectError('the Desktop endpoint descriptor is not valid JSON');
  }
  if (typeof descriptor !== 'object' || descriptor === null || Array.isArray(descriptor)) {
    throw new ConnectError('the Desktop endpoint descriptor is not a JSON object');
  }
  if (descriptor.protocol !== 1) {
    throw new ConnectError(`unsupported Desktop endpoint protocol ${String(descriptor.protocol)}`);
  }
  if (descriptor.backend !== 'desktop') {
    throw new ConnectError(`the endpoint descriptor is not a Desktop backend (${String(descriptor.backend)})`);
  }
  let url;
  try {
    url = new URL(String(descriptor.authenticatedUrl ?? ''));
  } catch {
    throw new ConnectError('the endpoint descriptor carries no usable authenticatedUrl');
  }
  // The launch URL must stay a bare loopback HTTP origin carrying exactly one
  // token: a tampered descriptor cannot send the token or the session cookie
  // to another host, another scheme, another path, or with extra parameters.
  if (url.protocol !== 'http:') throw new ConnectError('the Desktop launch URL must use plain http');
  if (url.username !== '' || url.password !== '') throw new ConnectError('the Desktop launch URL must not embed credentials');
  if (url.pathname !== '/') throw new ConnectError('the Desktop launch URL must be the bare origin root');
  if (url.hash !== '') throw new ConnectError('the Desktop launch URL must not contain a fragment');
  if (url.hostname === '') throw new ConnectError('the Desktop launch URL has no host');
  if (!isLoopbackName(url.hostname)) throw new ConnectError('the Desktop launch URL must point at loopback');
  if (url.port === '') throw new ConnectError('the Desktop launch URL must carry an explicit port');
  if (url.searchParams.getAll('token').length !== 1) throw new ConnectError('the Desktop launch URL must carry exactly one token');
  if ([...url.searchParams.keys()].length !== 1) throw new ConnectError('the Desktop launch URL must carry the token and nothing else');
  const token = url.searchParams.get('token');
  if (!token) throw new ConnectError('the Desktop launch URL carries an empty token');
  transportSecrets.add(token);
  transportSecrets.add(url.href);
  return {
    file,
    host: url.host,
    hostname: url.hostname === 'localhost' ? '127.0.0.1' : url.hostname,
    port: Number(url.port),
    origin: `http://${url.host}`,
    token,
    pid: Number.isSafeInteger(descriptor.pid) ? descriptor.pid : null,
    executable: typeof descriptor.executable === 'string' ? descriptor.executable : null,
    createdAt: typeof descriptor.createdAt === 'string' ? descriptor.createdAt : null,
  };
}

// ---------------------------------------------------------------------------
// HTTP: launch-token exchange and /api RPC
// ---------------------------------------------------------------------------

/** Exchange the process launch token for the Host's ordinary session cookie. */
function exchangeCookie(endpoint) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      host: endpoint.hostname,
      port: endpoint.port,
      method: 'GET',
      path: '/?token=' + encodeURIComponent(endpoint.token),
      headers: { host: endpoint.host, accept: 'text/html' },
    });
    request.setTimeout(CONNECT_TIMEOUT_MS, () => {
      request.destroy(new ConnectError(`the Desktop launch-token exchange timed out after ${CONNECT_TIMEOUT_MS}ms`));
    });
    request.on('error', (error) => {
      const detail = error instanceof ConnectError ? error.message
        : `cannot reach the Desktop Host at ${endpoint.origin} (${errorText(error)}); is DeepSeek Harness running?`;
      reject(new ConnectError(detail));
    });
    request.on('response', (response) => {
      const setCookie = response.headers['set-cookie'];
      response.resume();
      if (response.statusCode !== 303) {
        reject(new ConnectError(`the Desktop launch-token exchange answered HTTP ${String(response.statusCode)} (expected 303)`));
        return;
      }
      if (!Array.isArray(setCookie) || setCookie.length === 0) {
        reject(new ConnectError('the Desktop launch-token exchange returned no session cookie'));
        return;
      }
      const pair = String(setCookie[0]).split(';', 1)[0];
      const separator = pair.indexOf('=');
      const name = separator < 0 ? '' : pair.slice(0, separator);
      if (separator <= 0 || pair.length <= separator + 1 || !/^dsh-auth-[A-Za-z0-9_-]{43}$/.test(name)) {
        reject(new ConnectError('the Desktop launch-token exchange returned an unexpected cookie shape'));
        return;
      }
      transportSecrets.add(pair);
      transportSecrets.add(pair.slice(separator + 1));
      resolve(pair);
    });
    request.end();
  });
}

function readBody(response) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let bytes = 0;
    response.on('data', (chunk) => {
      bytes += chunk.length;
      if (bytes > MAX_RESPONSE_BYTES) {
        response.destroy(new ConnectError('the Desktop API response exceeded the safety limit'));
        return;
      }
      chunks.push(chunk);
    });
    response.on('error', reject);
    response.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
  });
}

/** One `/api/<method>` Remote call over the authenticated HTTP carrier. */
async function rpc(endpoint, cookie, method, args) {
  const rpcId = randomUUID();
  const body = JSON.stringify({ type: 'client-request', rpcId, method, payload: { args } });
  const text = await new Promise((resolve, reject) => {
    const request = http.request({
      host: endpoint.hostname,
      port: endpoint.port,
      method: 'POST',
      path: '/api/' + method,
      headers: {
        host: endpoint.host,
        origin: endpoint.origin,
        cookie,
        'content-type': 'application/json',
        'content-length': Buffer.byteLength(body),
      },
    });
    request.setTimeout(RPC_TIMEOUT_MS, () => {
      request.destroy(new ConnectError(`the Desktop API call ${method} timed out after ${RPC_TIMEOUT_MS}ms`));
    });
    request.on('error', reject);
    request.on('response', (response) => {
      if (!String(response.headers['content-type'] ?? '').toLowerCase().startsWith('application/json')) {
        response.resume();
        reject(new RemoteFailure(`the Desktop API call ${method} answered with ${String(response.headers['content-type'] ?? 'no content type')}`, 'gateway/transport'));
        return;
      }
      readBody(response).then(resolve, reject);
    });
    request.end(body);
  });

  let decoded;
  try {
    decoded = JSON.parse(text);
  } catch (error) {
    throw new RemoteFailure(`the Desktop API call ${method} did not answer JSON`, 'gateway/transport');
  }
  if (decoded?.type !== 'server-response' || decoded.rpcId !== rpcId) {
    throw new RemoteFailure(`the Desktop API call ${method} answered an unexpected envelope`, 'gateway/transport');
  }
  const result = decoded.result;
  if (result?.ok === true) return result.value;
  const failure = result?.error ?? {};
  throw new RemoteFailure(
    `${method}: ${String(failure.message ?? 'the Desktop Host refused the call')}`,
    String(failure.code ?? 'gateway/unknown'),
    failure.details,
  );
}

// ---------------------------------------------------------------------------
// Session helpers
// ---------------------------------------------------------------------------

async function ensureSession(endpoint, cookie, options) {
  const created = await rpc(endpoint, cookie, 'workspace/create', {
    request: { path: options.workspace },
  });
  const workspaceId = created?.workspace?.workspaceId;
  if (typeof workspaceId !== 'string' || workspaceId === '') {
    throw new RemoteFailure('workspace/create returned no workspace id', 'workspace/invalid-response');
  }
  emit({
    type: 'workspace',
    workspaceId,
    path: created.workspace.path,
    title: created.workspace.title,
    created: created.created === true,
  });
  const session = await rpc(endpoint, cookie, 'session/create', {
    request: { workspaceId },
  });
  const sessionId = session?.sessionId;
  if (typeof sessionId !== 'string' || sessionId === '') {
    throw new RemoteFailure('session/create returned no session id', 'session/invalid-response');
  }
  emit({ type: 'session', sessionId });
  if (options.title) {
    try {
      await rpc(endpoint, cookie, 'session/rename', {
        request: { sessionId, title: options.title },
      });
    } catch (error) {
      // A missing title service must not fail the task: the session id is what
      // matters, and an untitled session is still visible in Desktop.
      diag(`could not rename the Desktop session: ${errorText(error)}`);
    }
  }
  return { workspaceId, sessionId };
}

function canonicalWorkspace(value) {
  if (typeof value !== 'string' || !value) return null;
  const resolved = path.resolve(value);
  return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
}
function requireEmptyInbox(values) {
  const inbox = values?.inbox;
  if (inbox && (inbox['next-turn']?.length || inbox['next-step']?.length)) {
    throw new RemoteFailure('the Desktop session has queued messages; inspect it in Desktop before sending', 'session/agent-busy');
  }
}
async function assertSessionIdle(endpoint, cookie, sessionId) {
  const list = await rpc(endpoint, cookie, 'session/list', { _request: {} });
  const row = (list?.items ?? []).find((item) => item?.sessionId === sessionId);
  if (!row) throw new RemoteFailure('the bound Desktop session was not found', 'session/not-found');
  if (row?.running === true) {
    throw new RemoteFailure(
      `the Desktop session ${sessionId} is already running a turn; wait for it in Desktop (or cancel it there) before sending`,
      'session/agent-busy',
    );
  }
  if (canonicalWorkspace(row.cwd) !== canonicalWorkspace(process.cwd())) {
    throw new RemoteFailure('the Desktop session cwd does not match the bound project', 'session/workspace-mismatch');
  }
  requireEmptyInbox(row.projections?.values);
}

// ---------------------------------------------------------------------------
// Model catalog
// ---------------------------------------------------------------------------

function catalogSummary(catalog) {
  const groups = Array.isArray(catalog?.groups) ? catalog.groups : [];
  return {
    default: catalog?.default ?? null,
    routableProviders: Array.isArray(catalog?.routableProviders) ? catalog.routableProviders : [],
    groups: groups.map((group) => ({
      id: group?.id,
      name: group?.name,
      models: (Array.isArray(group?.models) ? group.models : []).map((model) => ({
        id: model?.id,
        name: model?.name,
        description: model?.description,
        reasoning: model?.reasoning === undefined || model?.reasoning === null ? null : {
          defaultEffort: model.reasoning?.defaultEffort,
          efforts: (model.reasoning?.efforts ?? []).map((effort) => ({ id: effort?.id, name: effort?.name })),
        },
      })),
    })),
  };
}

function validateSelection(catalog, selection) {
  const groups = Array.isArray(catalog?.groups) ? catalog.groups : [];
  const group = groups.find((candidate) => candidate?.id === selection.provider);
  if (group === undefined) {
    const known = groups.map((candidate) => candidate?.id).filter(Boolean).join(', ') || 'none';
    throw new UsageError(`unknown provider ${JSON.stringify(selection.provider)} (known: ${known}); run \`models\``);
  }
  const model = (group.models ?? []).find((candidate) => candidate?.id === selection.model);
  if (model === undefined) {
    const known = (group.models ?? []).map((candidate) => candidate?.id).filter(Boolean).join(', ') || 'none';
    throw new UsageError(`provider ${JSON.stringify(selection.provider)} has no model ${JSON.stringify(selection.model)} (known: ${known}); run \`models\``);
  }
  if (selection.reasoningEffort !== undefined) {
    const efforts = model.reasoning?.efforts ?? [];
    if (!efforts.some((effort) => effort?.id === selection.reasoningEffort)) {
      const known = efforts.map((effort) => effort?.id).filter(Boolean).join(', ') || 'none';
      throw new UsageError(`model ${JSON.stringify(selection.model)} has no reasoning effort ${JSON.stringify(selection.reasoningEffort)} (known: ${known})`);
    }
  }
}

// ---------------------------------------------------------------------------
// WebSocket follow stream
// ---------------------------------------------------------------------------

function loadWebSocket() {
  const candidates = [];
  if (process.env.DSH_DESKTOP_ASAR) candidates.push(process.env.DSH_DESKTOP_ASAR);
  for (const executable of [process.execPath, process.env.DSH_DESKTOP_EXECUTABLE]) {
    if (typeof executable !== 'string' || executable === '') continue;
    candidates.push(path.join(path.dirname(executable), 'resources', 'app.asar'));
  }
  for (const asar of candidates) {
    const manifest = path.join(asar, 'dsh', 'package.json');
    if (!fs.existsSync(manifest)) continue;
    try {
      const loaded = createRequire(manifest)('ws');
      const WebSocket = loaded.WebSocket ?? loaded;
      if (typeof WebSocket === 'function') return WebSocket;
    } catch (error) {
      diag(`could not load the bundled WebSocket client from ${asar}: ${errorText(error)}`);
    }
  }
  throw new ConnectError(
    'the WebSocket client bundled with Desktop could not be located; run this helper with the Desktop Electron binary in node mode',
  );
}

/** Follow one Desktop session and project its durable events onto callbacks. */
function openFollow(endpoint, cookie, sessionId, handlers) {
  const WebSocket = loadWebSocket();
  const streamId = randomUUID();
  const socket = new WebSocket(`ws://${endpoint.host}/api/remote.mux`, {
    origin: endpoint.origin,
    headers: { cookie },
    handshakeTimeout: CONNECT_TIMEOUT_MS,
  });
  let resolved = false;
  let resolveSnapshot;
  let rejectSnapshot;
  const snapshot = new Promise((resolve, reject) => {
    resolveSnapshot = resolve;
    rejectSnapshot = reject;
  });
  const fail = (error) => {
    if (resolved) return;
    resolved = true;
    rejectSnapshot(error);
  };
  socket.on('open', () => {
    socket.send(JSON.stringify({
      type: 'open',
      streamId,
      endpoint: 'session/follow',
      payload: { args: { request: { address: { kind: 'session', sessionId } } } },
    }));
  });
  socket.on('message', (data, isBinary) => {
    if (isBinary) { handlers.onStreamError({ code: 'gateway/transport', message: 'unexpected binary frame' }); return; }
    let message;
    try {
      message = JSON.parse(String(data));
    } catch {
      handlers.onStreamError({ code: 'gateway/transport', message: 'invalid stream JSON' });
      return;
    }
    if (message?.streamId !== streamId) return;
    if (message.type === 'item') {
      const frame = message.value;
      if (frame?.type === 'snapshot' && !resolved) {
        resolved = true;
        resolveSnapshot(frame);
        return;
      }
      handlers.onFrame(frame);
      return;
    }
    if (message.type === 'end') {
      // A server-side `end` is the orderly end of this logical stream, not a
      // transport failure. Before the snapshot it kills the subscription;
      // afterwards it only tells the turn that no further events will arrive.
      if (!resolved) {
        fail(new ConnectError('the Desktop follow stream ended before its snapshot'));
      } else if (typeof handlers.onEnded === 'function') {
        handlers.onEnded();
      }
      return;
    }
    if (message.type === 'error') {
      if (!resolved) {
        resolved = true;
        rejectSnapshot(new ConnectError(`the Desktop follow stream failed: ${String(message.error?.message ?? 'unknown')}`));
      }
      handlers.onStreamError(message.error);
    }
  });
  socket.on('error', (error) => {
    fail(new ConnectError(`the Desktop follow WebSocket failed: ${errorText(error)}`));
    handlers.onStreamError({ code: 'gateway/transport', message: errorText(error), details: {} });
  });
  socket.on('close', () => {
    fail(new ConnectError('the Desktop follow stream closed before its snapshot'));
    handlers.onClosed();
  });
  return {
    snapshot,
    close() {
      try {
        if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'cancel', streamId }));
      } catch { /* the socket is already gone */ }
      try {
        socket.close();
      } catch { /* the socket is already gone */ }
    },
  };
}

// ---------------------------------------------------------------------------
// Event projection
// ---------------------------------------------------------------------------

/** Concatenate only the visible `text` blocks of one message (never reasoning). */
function visibleText(message) {
  const content = message?.content;
  if (!Array.isArray(content)) return '';
  const parts = [];
  for (const block of content) {
    if (block?.type === 'text' && typeof block.text === 'string') parts.push(block.text);
  }
  return parts.join('');
}

function describeReason(reason) {
  if (reason === undefined || reason === null) return 'unknown';
  if (typeof reason === 'string') return reason;
  const kind = String(reason.kind ?? 'unknown');
  if (reason.kind === 'error') return `error: ${String(reason.error?.message ?? reason.error?.name ?? 'model failure')}`;
  if (reason.kind === 'aborted') return `aborted: ${String(reason.reason?.kind ?? reason.reason ?? 'cancelled')}`;
  return kind;
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

async function commandDoctor(endpoint) {
  const cookie = await exchangeCookie(endpoint);
  const catalog = await rpc(endpoint, cookie, 'session/modelCatalog', {});
  emit({
    type: 'result',
    value: {
      connected: true,
      host: endpoint.host,
      pid: endpoint.pid,
      default: catalog?.default ?? null,
      providers: (catalog?.groups ?? []).map((group) => group?.id).filter(Boolean),
    },
  });
}

async function commandModels(endpoint) {
  const cookie = await exchangeCookie(endpoint);
  const catalog = await rpc(endpoint, cookie, 'session/modelCatalog', {});
  emit({ type: 'result', value: catalogSummary(catalog) });
}

async function commandPrepare(endpoint, request) {
  const cookie = await exchangeCookie(endpoint);
  const created = await ensureSession(endpoint, cookie, {
    workspace: request.workspace,
    title: request.title,
  });
  emit({ type: 'result', value: { ...created, workspace: request.workspace } });
}

async function commandSelectModel(endpoint, request) {
  const cookie = await exchangeCookie(endpoint);
  const catalog = await rpc(endpoint, cookie, 'session/modelCatalog', {});
  validateSelection(catalog, request);
  const selected = await rpc(endpoint, cookie, 'session/selectModel', {
    request: {
      sessionId: request.sessionId,
      provider: request.provider,
      model: request.model,
      ...request.reasoningEffort === undefined ? {} : { reasoningEffort: request.reasoningEffort },
    },
  });
  emit({ type: 'result', value: { selected: selected?.selected ?? null } });
}

async function commandTurn(endpoint, cookie, request, control) {
  const requireNotCancelled = () => {
    if (control.cancelled) throw new RemoteFailure('the bridge request was cancelled before submission', 'turn/cancelled');
  };
  requireNotCancelled();
  let sessionId = typeof request.sessionId === 'string' && request.sessionId !== '' ? request.sessionId : null;
  let created = false;
  if (request.create !== undefined && request.create !== null) {
    const result = await ensureSession(endpoint, cookie, {
      workspace: request.create.workspace,
      title: request.create.title,
    });
    sessionId = result.sessionId;
    created = true;
  }
  if (sessionId === null) throw new UsageError('turn requires sessionId, or a create block');
  // A resumed turn must report its session exactly like a created one, so the
  // parent can persist and verify it.
  if (!created) emit({ type: 'session', sessionId });
  if (typeof request.text !== 'string' || request.text.trim() === '') {
    throw new UsageError('turn requires non-empty text');
  }
  const requestId = typeof request.requestId === 'string' && request.requestId !== '' ? request.requestId : randomUUID();
  const clientTimeZone = typeof request.clientTimeZone === 'string' ? request.clientTimeZone : DEFAULT_TIME_ZONE;

  control.sessionId = sessionId;
  await assertSessionIdle(endpoint, cookie, sessionId);
  requireNotCancelled();

  const outcome = { settled: false };
  const done = new Promise((resolve, reject) => {
    outcome.resolve = resolve;
    outcome.reject = reject;
  });
  // The stream can settle before `await done` is reached; never let that turn
  // into an unhandled rejection that would kill the helper.
  done.catch(() => {});
  const settle = (error, reason) => {
    if (outcome.settled) return;
    outcome.settled = true;
    if (error) outcome.reject(error);
    else outcome.resolve(reason);
  };

  let openTurn = null;
  let myUserSeq = null;
  let myTurn = null;
  let pendingUserSeq = null;
  let lastAssistantText = '';
  let sawApproval = false;

  const follow = openFollow(endpoint, cookie, sessionId, {
    onFrame(frame) {
      if (frame?.type !== 'event') return;
      const event = frame.event;
      if (event === undefined || event === null) return;
      const data = event.data ?? {};
      switch (event.type) {
        case 'user/message': {
          const source = data.source ?? {};
          if (myUserSeq === null && source.kind === 'user' && source.rpcId === requestId) {
            myUserSeq = event.seq;
            control.ownsTurn = true;
            if (openTurn !== null) myTurn = openTurn;
            else pendingUserSeq = event.seq;
          } else if (source.kind === 'user' && source.rpcId !== requestId) {
            control.interference = true;
            settle(new RemoteFailure('another user message entered the Desktop session during this request; inspect the session before continuing', 'session/interference'));
          }
          break;
        }
        case 'turn/start': {
          openTurn = data.turn;
          emit({ type: 'status', phase: 'turn_start', turn: data.turn });
          if (pendingUserSeq !== null && pendingUserSeq < event.seq) {
            myTurn = data.turn;
            pendingUserSeq = null;
          }
          break;
        }
        case 'turn/end': {
          if (openTurn === data.turn) openTurn = null;
          emit({ type: 'turn_end', turn: data.turn, reason: data.reason ?? null });
          if (myTurn !== null && data.turn === myTurn) {
            control.ownTurnEnded = true;
            settle(null, data.reason ?? { kind: 'unknown' });
          }
          break;
        }
        case 'request/header': {
          const config = data.header?.config;
          if (myUserSeq !== null && config !== undefined && config !== null) {
            emit({
              type: 'header',
              provider: config.provider,
              model: config.model,
              ...config.reasoningEffort === undefined ? {} : { reasoningEffort: config.reasoningEffort },
            });
          }
          break;
        }
        case 'assistant/message': {
          if (myTurn !== null && data.turn === myTurn) {
            const text = visibleText(data.message);
            if (text !== '') {
              lastAssistantText = text;
              emit({ type: 'text', text });
            }
          }
          break;
        }
        case 'tool/call': {
          if (myTurn !== null && data.turn === myTurn) {
            let args = data.arguments;
            if (typeof args === 'string') {
              try { args = JSON.parse(args); } catch { args = '[unstructured arguments omitted; view Desktop]'; }
            }
            emit({ type: 'tool_call', callId: data.callId, name: data.name, arguments: args });
          }
          break;
        }
        case 'tool/result': {
          if (myTurn !== null && data.turn === myTurn) {
            emit({
              type: 'tool_result',
              callId: data.message?.toolCallId,
              isError: data.message?.isError === true || data.error !== undefined,
              // Raw tool output can include files or credentials. Desktop owns
              // that display; the collaboration log needs the outcome only.
              text: '[tool output available in Desktop]',
            });
          }
          break;
        }
        case 'approval/asked': {
          sawApproval = true;
          emit({ type: 'approval', id: data.id, toolName: data.toolName, reason: data.reason });
          break;
        }
        default:
          break;
      }
    },
    onStreamError(error) {
      settle(new ConnectError(`the Desktop follow stream failed: ${String(error?.message ?? 'unknown')}`));
    },
    onEnded() {
      // `end` before our turn/end means the stream is gone and the turn can no
      // longer be confirmed. It is a no-op once the turn already settled.
      settle(new ConnectError('the Desktop follow stream ended before the turn ended'));
    },
    onClosed() {
      if (!outcome.settled) settle(new ConnectError('the Desktop follow stream closed before the turn ended'));
    },
  });

  control.follow = follow;
  try {
    const snapshot = await follow.snapshot;
    if (snapshot?.header?.id !== sessionId || canonicalWorkspace(snapshot?.header?.cwd) !== canonicalWorkspace(process.cwd())) {
      throw new RemoteFailure('Desktop subscription does not match the bound session and project', 'session/workspace-mismatch');
    }
    requireEmptyInbox(snapshot.projections?.values);
    emit({ type: 'status', phase: 'subscribed', cursor: snapshot?.cursor ?? null });
    requireNotCancelled();
    if (outcome.settled) await done;
    control.promptSent = true;
    const accepted = await rpc(endpoint, cookie, 'session/prompt', {
      request: {
        requestId,
        sessionId,
        mode: 'queue',
        content: [{ type: 'text', text: request.text }],
        clientTimeZone,
      },
    });
    if (accepted?.accepted !== true) {
      throw new RemoteFailure('the Desktop Host did not accept the prompt', 'session/prompt-rejected');
    }
    control.promptAccepted = true;
    emit({ type: 'status', phase: 'prompt_accepted', requestId });

    const reason = await done;
    if (control.cancelled) { await control.cancelPromise; return; }
    const kind = String(reason?.kind ?? 'unknown');
    if (kind !== 'completed') {
      const hint = sawApproval
        ? '; the Desktop session was waiting for an approval that only the user can grant in Desktop'
        : '';
      throw new RemoteFailure(`the Desktop turn ended as ${describeReason(reason)}${hint}`, `turn/${kind}`);
    }
    if (lastAssistantText === '') {
      throw new RemoteFailure('the Desktop turn completed without any assistant text', 'turn/empty-reply');
    }
    emit({ type: 'final', text: lastAssistantText });
  } finally {
    follow.close();
  }
}

// ---------------------------------------------------------------------------
// stdin plumbing
// ---------------------------------------------------------------------------

function readRequest() {
  return new Promise((resolve, reject) => {
    let buffered = '';
    let consumed = false;
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => {
      if (consumed) return;
      buffered += chunk;
      const newline = buffered.indexOf('\n');
      if (newline < 0) return;
      consumed = true;
      const line = buffered.slice(0, newline).trim();
      if (line === '') {
        reject(new UsageError('expected one JSON request line on stdin'));
        return;
      }
      try {
        resolve(JSON.parse(line));
      } catch (error) {
        reject(new UsageError('the stdin request is not valid JSON'));
      }
    });
    process.stdin.on('end', () => {
      if (!consumed) reject(new UsageError('expected one JSON request line on stdin'));
    });
    process.stdin.on('error', (error) => reject(new UsageError(`cannot read stdin: ${errorText(error)}`)));
  });
}

function watchCancellation(control) {
  let buffered = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => {
    buffered += chunk;
    for (;;) {
      const newline = buffered.indexOf('\n');
      if (newline < 0) break;
      const line = buffered.slice(0, newline).trim();
      buffered = buffered.slice(newline + 1);
      if (line === '') continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        continue;
      }
      if (message?.cmd === 'cancel') control.cancel();
    }
  });
  process.stdin.on('error', () => { /* the parent is gone; the turn settles on its own */ });
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

async function cancelTurn(endpoint, cookie, control) {
  const sessionId = control.sessionId;
  control.cancelled = true;
  if (sessionId === null || !control.promptSent) {
    emit({
      type: 'cancel',
      requested: true,
      ok: false,
      stopped: true,
      detail: 'the bridge did not submit a Desktop prompt',
    });
    emit({
      type: 'error',
      code: 'turn/cancelled',
      message: 'the bridge was cancelled before a Desktop prompt was submitted',
    });
    finish(EXIT_CANCELLED);
    return;
  }
  let ok = false;
  let stopped = control.ownTurnEnded === true;
  let detail;
  if (!control.ownsTurn || control.interference || stopped) {
    detail = stopped ? 'the owned turn already ended' : 'cannot safely attribute the current turn to this request; cancellation was not sent';
  } else try {
    const accepted = await rpc(endpoint, cookie, 'session/cancel', {
      request: { sessionId },
    });
    ok = accepted?.accepted === true;
    detail = ok
      ? 'session/cancel was accepted; waiting for the owned turn to end'
      : 'the Desktop Host did not acknowledge session/cancel';
  } catch (error) {
    detail = `session/cancel failed: ${errorText(error)}`;
  }
  if (ok) {
    const until = Date.now() + 2000;
    while (!control.ownTurnEnded && Date.now() < until) await new Promise(resolve => setTimeout(resolve, 50));
    stopped = control.ownTurnEnded === true;
    detail = stopped ? 'the owned Desktop turn ended after cancellation' : 'cancellation was requested; the owned turn has not confirmed stopping';
  }
  emit({ type: 'cancel', requested: true, ok, stopped, detail, sessionId });
  emit({
    type: 'error',
    code: 'turn/cancelled',
    message: stopped
      ? 'the bridge deadline expired; the Desktop turn was cancelled'
      : `the bridge was cancelled but stopping did not confirm; the Desktop task may still be running (${detail})`,
  });
  try {
    control.follow?.close();
  } catch { /* already closed */ }
  finish(EXIT_CANCELLED);
}

async function main(argv) {
  const [endpointFile, command, ...rest] = argv;
  if (endpointFile === undefined || command === undefined || rest.length > 0) {
    throw new UsageError('usage: dsh_desktop.cjs <endpoint.json> <doctor|models|prepare|select-model|turn>');
  }
  const endpoint = readEndpoint(endpointFile);

  if (command === 'turn') {
    const control = { sessionId: null, follow: null, cancelled: false, promptAccepted: false, cancel: () => {} };
    const cookie = await exchangeCookie(endpoint);
    control.cancel = () => {
      if (!control.cancelled) control.cancelPromise = cancelTurn(endpoint, cookie, control);
    };
    watchCancellation(control);
    const request = await readRequest();
    await commandTurn(endpoint, cookie, request, control);
    finish(EXIT_OK);
    return;
  }

  if (command === 'doctor') await commandDoctor(endpoint);
  else if (command === 'models') await commandModels(endpoint);
  else if (command === 'prepare') await commandPrepare(endpoint, await readRequest());
  else if (command === 'select-model') await commandSelectModel(endpoint, await readRequest());
  else throw new UsageError(`unknown command ${JSON.stringify(command)}`);
  finish(EXIT_OK);
}

main(process.argv.slice(2)).catch((error) => {
  if (error instanceof UsageError) {
    emit({ type: 'error', code: 'usage', message: error.message });
    finish(EXIT_USAGE);
  } else if (error instanceof ConnectError) {
    emit({ type: 'error', code: 'connect', message: error.message });
    finish(EXIT_CONNECT);
  } else if (error instanceof RemoteFailure) {
    emit({ type: 'error', code: error.code, message: error.message, details: error.details ?? null });
    finish(EXIT_TURN);
  } else {
    emit({ type: 'error', code: 'internal', message: `${error?.name ?? 'Error'}: ${errorText(error)}` });
    finish(EXIT_TURN);
  }
});
