import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** Serve production assets by interception only: no HTTP server or real backend. */
export async function mockBuiltFrontend(page, directory = process.env.TEST_DIST, origin = 'https://frontend.synthetic.test') {
  const root = path.resolve(directory || fileURLToPath(new URL('../frontend/dist', import.meta.url)));
  const unexpected = [];
  const mime = { '.html': 'text/html', '.js': 'application/javascript', '.mjs': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.png': 'image/png' };
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== origin || url.pathname.startsWith('/api/')) {
      unexpected.push(`${request.method()} ${url.origin}${url.pathname}`);
      return route.abort('blockedbyclient');
    }
    const asset = path.resolve(root, `.${decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname)}`);
    if (!asset.startsWith(root + path.sep)) throw new Error('Unexpected asset path');
    await route.fulfill({ contentType: mime[path.extname(asset)] || 'application/octet-stream', body: await readFile(asset) });
  });
  return { origin, unexpected };
}
