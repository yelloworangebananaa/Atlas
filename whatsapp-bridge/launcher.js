import { spawn, spawnSync } from 'node:child_process';
import { execPath } from 'node:process';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
// Uses the repo's venv Python by default (../.venv, created by setup.py); override with $PYTHON.
const PYTHON =
    process.env.PYTHON ??
    path.join(ROOT, '..', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');

// --use-system-ca (corporate/antivirus TLS proxies) only exists on newer Node — probe once.
const systemCa = spawnSync(execPath, ['--use-system-ca', '-e', '0']).status === 0
    ? ['--use-system-ca'] : [];

// Email notifier only runs when Gmail is configured in .env — and if it dies, the
// bridge stays up (it's an optional extra, not a dependency).
let env = '';
try { env = fs.readFileSync(path.join(ROOT, '.env'), 'utf8'); } catch { /* no .env yet */ }
const hasGmail = /^\s*GMAIL_USER\s*=\s*\S/m.test(env);

console.log(`[launcher] starting WhatsApp bridge${hasGmail ? ' + email notifier' : ''}`);

const bridge = spawn(execPath, [...systemCa, 'index.js'], { cwd: ROOT, stdio: 'inherit' });
let notifier = null;

bridge.on('exit', (code, signal) => {
    console.log(`[launcher] bridge exited (${signal ?? code})`);
    if (notifier && !notifier.killed) notifier.kill();
    process.exit(code ?? 1);
});

if (hasGmail) {
    notifier = spawn(PYTHON, [path.join(ROOT, 'email-notifier.py')], { cwd: ROOT, stdio: 'inherit' });
    notifier.on('exit', (code, signal) =>
        console.log(`[launcher] email-notifier exited (${signal ?? code}) — bridge stays up`));
    notifier.on('error', (err) =>
        console.log(`[launcher] email-notifier failed to start (${err.message}) — bridge stays up`));
}

process.on('SIGINT', () => { bridge.kill('SIGINT'); notifier?.kill('SIGINT'); });
process.on('SIGTERM', () => { bridge.kill(); notifier?.kill(); });
