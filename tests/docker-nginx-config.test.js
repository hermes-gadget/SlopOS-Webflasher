import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const compose = readFileSync(new URL('../compose.yml', import.meta.url), 'utf8');
const nginx = readFileSync(new URL('../nginx.conf', import.meta.url), 'utf8');
const headers = readFileSync(new URL('../nginx-security-headers.conf', import.meta.url), 'utf8');

test('compose runs a proxied backend with isolated vault permissions', () => {
  assert.match(compose, /sigurdos-backend:/);
  assert.match(compose, /SIGURDOS_FIRMWARE_VAULT: \/vault/);
  assert.match(compose, /\$\{SIGURDOS_FIRMWARE_VAULT:-\.\/vault\}:\/vault:ro/);
  assert.match(compose, /firmware-sync:/);
  assert.match(compose, /profiles:\n\s+- sync/);
});

test('nginx proxies API and firmware routes and preserves headers in cached locations', () => {
  assert.match(nginx, /location \/api\//);
  assert.match(nginx, /location ~ \^\/\(latest\|dev\|debug\|archive\)\//);
  assert.equal((nginx.match(/include \/etc\/nginx\/security-headers\.conf;/g) || []).length, 4);
  assert.match(headers, /add_header Content-Security-Policy/);
  assert.match(headers, /add_header X-Content-Type-Options/);
});
