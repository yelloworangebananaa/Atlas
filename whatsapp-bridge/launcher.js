import { spawn } from 'node:child_process';
import { execPath } from 'node:process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
// Uses the repo's venv Python by default (../.venv, created by setup.py); override with $PYTHON.
const PYTHON =
    process.env.PYTHON ??
    path.join(ROOT, '..', '.venv', 'Scripts', 'python.exe');

const children = [];

function start(name, command, args) {
    const child = spawn(command, args, { cwd: ROOT, stdio: 'inherit' });
    child.on('exit', (code, signal) => {
        console.log(`[launcher] ${name} exited (${signal ?? code})`);
        shutdown(code ?? 1);
    });
    children.push(child);
}

function shutdown(code = 0) {
    for (const child of children) {
        if (!child.killed) child.kill();
    }
    process.exit(code);
}

process.on('SIGINT', () => shutdown(0));
process.on('SIGTERM', () => shutdown(0));

console.log('[launcher] starting WhatsApp bridge + email notifier');
start('bridge', execPath, ['--use-system-ca', 'index.js']);
start('email-notifier', PYTHON, [path.join(ROOT, 'email-notifier.py')]);
