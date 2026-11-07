// User-installed Desktop plugin: publish the Host's normal authenticated launch
// URL to a private, local bridge descriptor. No browser cookies or API keys are
// read, and the Host's authentication and session permissions remain enabled.
import { mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute } from 'node:path';
import { randomUUID } from 'node:crypto';

export const name = 'codex-dsh-desktop-endpoint';
export const inject = ['connection', 'webServer'];

export async function apply(ctx, config) {
  if (!config || !isAbsolute(config.endpointFile)) throw new Error('An absolute endpointFile is required');
  const port = ctx.webServer.port;
  if (!Number.isInteger(port) || port < 1) throw new Error('Desktop HTTP server is not ready');
  const owner = randomUUID();
  const descriptor = {
    protocol: 1,
    backend: 'desktop',
    pid: process.pid,
    executable: process.execPath,
    owner,
    createdAt: new Date().toISOString(),
    authenticatedUrl: ctx.connection.authenticatedUrl(`http://127.0.0.1:${port}/`),
  };
  await mkdir(dirname(config.endpointFile), { recursive: true, mode: 0o700 });
  const temporary = `${config.endpointFile}.${owner}.tmp`;
  try {
    await writeFile(temporary, JSON.stringify(descriptor), { mode: 0o600, flag: 'wx' });
    await rename(temporary, config.endpointFile);
  } finally {
    await unlink(temporary).catch(() => {});
  }
  ctx.effect(() => async () => {
    try {
      const current = JSON.parse(await readFile(config.endpointFile, 'utf8'));
      if (current.owner === owner) await unlink(config.endpointFile);
    } catch { /* The descriptor may already have been replaced or removed. */ }
  });
}
