import fs from 'fs';
import path from 'path';

const STORE_PATH = path.join(process.cwd(), 'messages.jsonl');
const MAX_MEMORY = 500;

/** @type {Array<object>} */
let cache = [];

function loadCache() {
    if (!fs.existsSync(STORE_PATH)) return;
    const lines = fs.readFileSync(STORE_PATH, 'utf8').split('\n').filter(Boolean);
    cache = lines.slice(-MAX_MEMORY).map((line) => JSON.parse(line));
}

loadCache();

export function appendMessage(record) {
    cache.push(record);
    if (cache.length > MAX_MEMORY) {
        cache = cache.slice(-MAX_MEMORY);
    }
    fs.appendFileSync(STORE_PATH, `${JSON.stringify(record)}\n`, 'utf8');
    return record;
}

export function listMessages({ since = 0, limit = 50, unread = false, jid = null } = {}) {
    let rows = cache.filter((m) => m.timestamp >= since);
    if (unread) rows = rows.filter((m) => m.direction === 'inbound' && !m.readByAi);
    if (jid) {
        const needle = jid.includes('@') ? jid : `${jid.replace(/\D/g, '')}@s.whatsapp.net`;
        rows = rows.filter((m) => m.jid === needle || m.phoneJid === needle);
    }
    return rows.slice(-limit);
}

export function getLatest(limit = 20) {
    return cache.slice(-limit);
}

export function markRead({ ids = [], all = false } = {}) {
    let count = 0;
    for (const row of cache) {
        if (row.direction !== 'inbound') continue;
        if (all || ids.includes(row.id)) {
            if (!row.readByAi) count++;
            row.readByAi = true;
        }
    }
    if (count > 0) {
        fs.writeFileSync(STORE_PATH, `${cache.map((r) => JSON.stringify(r)).join('\n')}\n`, 'utf8');
    }
    return count;
}

export function getUnreadCount() {
    return cache.filter((m) => m.direction === 'inbound' && !m.readByAi).length;
}

/** Oldest stored message for a contact — anchor for fetchMessageHistory. */
export function getMessageAnchorForContact({ phoneJid, lid, jid } = {}) {
    const needles = new Set([phoneJid, lid, jid].filter(Boolean));
    let oldest = null;
    for (const m of cache) {
        const matches =
            needles.has(m.jid) || needles.has(m.phoneJid) || needles.has(m.lid);
        if (!matches) continue;
        if (!oldest || m.timestamp < oldest.timestamp) oldest = m;
    }
    return oldest;
}
