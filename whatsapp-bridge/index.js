import makeWASocket, {
    DisconnectReason,
    useMultiFileAuthState,
    makeCacheableSignalKeyStore,
    fetchLatestBaileysVersion,
    downloadMediaMessage,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import express from 'express';
import pino from 'pino';
import qrcode from 'qrcode-terminal';
import * as messageStore from './message-store.js';
import {
    toJid,
    resolveJid,
    seedLidMapping,
    normalizeWaMessage,
    isAllowedSender,
    jidToPhone,
    lidToPhoneJid,
} from './message-utils.js';
import {
    getPrivacyTokenStatus,
    logPrivacyTokenStatus,
    waitForPrivacyToken,
    syncPrivacyTokenFromPhone,
} from './tc-token.js';

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const AUTH_DIR = 'auth_info_multi';
const RECONNECT_DELAY_MS = 5000;
const WEBHOOK_URL = process.env.WEBHOOK_URL || null;
const JARVIS_URL = process.env.JARVIS_URL || 'http://127.0.0.1:18923';
const JARVIS_AUTO_REPLY = process.env.JARVIS_AUTO_REPLY !== 'false';

// Comma-separated JIDs or phone numbers allowed to send inbound messages to the AI.
// Empty = allow all.
const ALLOWED_JIDS = (process.env.ALLOWED_JIDS || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

const logger = pino({ level: 'warn' });

let whatsappSocket = null;
let isConnected = false;
let connecting = false;
let linkedAccount = null;
let lastConflictAt = 0;
let stableSince = 0;

/** Track outbound message IDs so we don't re-ingest our own sends (OpenClaw pattern). */
const outboundIds = new Set();
const MAX_OUTBOUND_TRACK = 500;

function rememberOutbound(id) {
    if (!id) return;
    outboundIds.add(id);
    if (outboundIds.size > MAX_OUTBOUND_TRACK) {
        outboundIds.delete(outboundIds.values().next().value);
    }
}

function contactAnchor(phoneJid, lid, jid) {
    return messageStore.getMessageAnchorForContact({ phoneJid, lid, jid });
}

async function ensurePeerPrivacyToken(sock, jid, phoneJid, lid) {
    let status = await getPrivacyTokenStatus(sock, jid);
    if (status.ready) return status;

    status = await waitForPrivacyToken(sock, jid, 2000);
    if (status.ready) return status;

    const anchor = contactAnchor(phoneJid, lid, jid);
    return syncPrivacyTokenFromPhone(sock, jid, anchor);
}

const KNOWN_LID_MAPPINGS = [
];

/** Wait for delivery receipt (status >= 2), error, or timeout. */
function waitForDelivery(sock, messageId, timeoutMs = 15000) {
    return new Promise((resolve) => {
        const timer = setTimeout(() => {
            sock.ev.off('messages.update', handler);
            resolve(null);
        }, timeoutMs);

        function handler(updates) {
            for (const { key, update } of updates) {
                if (key.id === messageId && key.fromMe && update.status != null) {
                    if (update.status >= 2) {
                        clearTimeout(timer);
                        sock.ev.off('messages.update', handler);
                        resolve(update.status);
                    } else if (update.status === 0) {
                        clearTimeout(timer);
                        sock.ev.off('messages.update', handler);
                        resolve('error');
                    }
                }
            }
        }

        sock.ev.on('messages.update', handler);
    });
}

async function notifyWebhook(record) {
    if (!WEBHOOK_URL) return;
    try {
        await fetch(WEBHOOK_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ event: 'message.inbound', message: record }),
        });
    } catch (err) {
        console.error('Webhook delivery failed:', err.message);
    }
}

// Media message kinds we download and forward to Jarvis as real file bytes.
const MEDIA_TYPES = ['imageMessage', 'documentMessage', 'audioMessage', 'videoMessage', 'stickerMessage'];
const MEDIA_EXT = { imageMessage: 'jpg', documentMessage: 'bin', audioMessage: 'ogg', videoMessage: 'mp4', stickerMessage: 'webp' };

/** If msg carries media (photo/PDF/voice/etc.), download it to a base64 attachment
 *  {name, mime, data, caption}. Returns null for a plain text message. */
async function extractMedia(sock, msg) {
    const m = msg.message || {};
    // unwrap ephemeral / view-once wrappers so the media node is reachable
    const inner = m.ephemeralMessage?.message || m.viewOnceMessage?.message
        || m.viewOnceMessageV2?.message || m.viewOnceMessageV2Extension?.message || m;
    const mtype = MEDIA_TYPES.find((t) => inner[t]);
    if (!mtype) return null;
    const node = inner[mtype];
    try {
        const buf = await downloadMediaMessage(
            { key: msg.key, message: inner }, 'buffer', {},
            { logger, reuploadRequest: sock.updateMediaMessage },
        );
        const name = node.fileName || node.title || `${mtype.replace('Message', '')}.${MEDIA_EXT[mtype] || 'bin'}`;
        return { name, mime: node.mimetype || '', data: buf.toString('base64'), caption: node.caption || '' };
    } catch (e) {
        console.error(`[media] download failed for ${mtype}:`, e.message);
        return null;
    }
}

async function replyViaJarvis(sock, record, msg) {
    if (!JARVIS_AUTO_REPLY) return;
    const label = jidToPhone(record.phoneJid) ?? record.jid;
    const media = msg ? await extractMedia(sock, msg) : null;
    const attachments = media ? [{ name: media.name, mime: media.mime, data: media.data }] : [];
    // With a real file attached, prompt is its caption (if any) — NOT the "[document] name.pdf"
    // placeholder; Jarvis reads the file itself. Plain text messages use record.text as before.
    const message = (media ? media.caption : record.text ?? '').trim();
    if (!message && !attachments.length) return;
    if (media) {
        console.log(`[media] ${label}: ${media.name} (${media.mime || 'unknown'}, ~${Math.round(media.data.length * 0.75 / 1024)}KB)`);
    }
    try {
        const res = await fetch(`${JARVIS_URL}/api/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message, attachments }),
            signal: AbortSignal.timeout(180_000),  // media + LLM can be slower than plain text
        });
        const { reply } = await res.json();
        if (!reply?.trim()) return;
        const dest = record.lid ?? record.jid ?? record.phoneJid;
        const sent = await sock.sendMessage(dest, { text: reply.trim() });
        rememberOutbound(sent?.key?.id);
        console.log(`[jarvis→wa] ${label}: ${reply.trim().slice(0, 100)}`);
    } catch (err) {
        console.error(`[jarvis] auto-reply failed for ${label}:`, err.message);
    }
}

function attachMessageHandlers(sock) {
    sock.ev.on('messages.update', (updates) => {
        for (const { key, update } of updates) {
            if (update.status != null && key.fromMe) {
                const labels = { 0: 'error', 1: 'pending', 2: 'sent', 3: 'delivered', 4: 'read' };
                const label = labels[update.status] ?? update.status;
                console.log(`Delivery ${key.id?.slice(0, 8)}… → ${label}`);
                if (update.messageStubParameters?.includes('463')) {
                    console.warn(
                        'Error 463: peer privacy token missing or account restricted — ' +
                            'wait for history sync, have contact message again, or send once from phone app.'
                    );
                }
            }
        }
    });

    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify' && type !== 'append') return;

        for (const msg of messages) {
            if (!msg.message) continue;

            // Skip echoes of our own outbound sends
            if (msg.key.fromMe && outboundIds.has(msg.key.id)) {
                outboundIds.delete(msg.key.id);
                continue;
            }

            const direction = msg.key.fromMe ? 'outbound' : 'inbound';
            const record = normalizeWaMessage(msg, direction);
            if (!record) continue;

            if (direction === 'inbound' && !isAllowedSender(record, ALLOWED_JIDS)) {
                console.log(`Blocked inbound from ${record.jid} (not in allowlist)`);
                continue;
            }

            messageStore.appendMessage(record);
            const label = jidToPhone(record.phoneJid) ?? record.jid;
            console.log(`[${direction}] ${label}: ${record.text}`);

            if (direction === 'inbound') {
                await notifyWebhook(record);
                replyViaJarvis(sock, record, msg);
                const jidForToken = record.lid ?? record.phoneJid ?? record.jid;
                const anchor = contactAnchor(record.phoneJid, record.lid, record.jid);
                ensurePeerPrivacyToken(sock, jidForToken, record.phoneJid, record.lid).then((status) => {
                    logPrivacyTokenStatus(`after inbound from ${label}`, status);
                });
            }
        }
    });
}

async function connectToWhatsApp() {
    if (connecting) return whatsappSocket;
    connecting = true;

    try {
        const { version } = await fetchLatestBaileysVersion();
        const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

        const baseKeys = state.keys;
        const origSet = baseKeys.set.bind(baseKeys);
        baseKeys.set = async (data) => {
            if (data?.tctoken) {
                for (const [jid, entry] of Object.entries(data.tctoken)) {
                    if (jid === '__index') continue;
                    const bytes = entry?.token?.length ?? 0;
                    if (bytes > 0) {
                        console.log(`[tctoken] stored ${bytes} bytes for ${jid}`);
                    }
                }
            }
            return origSet(data);
        };

        const sock = makeWASocket({
            version,
            auth: {
                creds: state.creds,
                keys: makeCacheableSignalKeyStore(state.keys, logger),
            },
            logger,
            browser: ['Jarvis Bridge', 'Chrome', '1.0.0'],
            markOnlineOnConnect: true,
            syncFullHistory: true,
            shouldSyncHistoryMessage: () => true,
        });

        sock.ev.on('creds.update', saveCreds);

        sock.ev.on('lid-mapping.update', ({ lid, pn }) => {
            if (!lid || !pn) return;
            console.log(`LID mapping: ${pn} ↔ ${lid}`);
            lidToPhoneJid.set(lid, pn);
        });

        attachMessageHandlers(sock);

        sock.ev.on('messaging-history.status', ({ syncType, progress, isLatest }) => {
            console.log(`[history] status: ${syncType ?? 'unknown'} progress=${progress ?? '?'}${isLatest ? ' (latest)' : ''}`);
        });

        sock.ev.on('messaging-history.set', ({ chats, isLatest }) => {
            const withToken = (chats ?? []).filter((c) => c.tcToken?.length);
            console.log(
                `[history] sync chunk: ${chats?.length ?? 0} chats` +
                    (withToken.length ? `, ${withToken.length} with tctoken` : '') +
                    (isLatest ? ' (latest)' : '')
            );
            for (const chat of withToken) {
                console.log(`[history] tctoken synced for chat ${chat.id}`);
            }
        });

        sock.ev.on('connection.update', (update) => {
            const { connection, lastDisconnect, qr } = update;

            if (qr) {
                console.log('\nScan the QR code with WhatsApp (Linked Devices):\n');
                qrcode.generate(qr, { small: true });
            }

            if (connection === 'close') {
                isConnected = false;
                whatsappSocket = null;
                connecting = false;
                stableSince = 0;

                const statusCode = (lastDisconnect?.error instanceof Boom)
                    ? lastDisconnect.error.output?.statusCode
                    : undefined;
                const reason = lastDisconnect?.error?.message ?? 'unknown';

                if (statusCode === DisconnectReason.connectionReplaced) {
                    lastConflictAt = Date.now();
                    console.log('\n*** SESSION CONFLICT ***');
                    console.log('Another WhatsApp Web session is active and kicked this bridge.');
                    console.log('On your phone: WhatsApp → Linked devices → log out ALL other sessions.');
                    console.log('Then restart: npm start\n');
                }

                const shouldReconnect =
                    statusCode !== DisconnectReason.loggedOut &&
                    statusCode !== DisconnectReason.connectionReplaced;
                console.log(`Connection closed (${statusCode ?? 'no code'}): ${reason}`);

                if (shouldReconnect) {
                    console.log(`Reconnecting in ${RECONNECT_DELAY_MS / 1000}s...`);
                    setTimeout(() => connectToWhatsApp(), RECONNECT_DELAY_MS);
                } else {
                    console.log('Logged out. Delete auth_info_multi and restart to pair again.');
                }
            } else if (connection === 'open') {
                whatsappSocket = sock;
                isConnected = true;
                connecting = false;
                stableSince = Date.now();
                const me = sock.user ?? state.creds.me;
                linkedAccount = me ? {
                    jid: me.id,
                    lid: me.lid ?? null,
                    phone: jidToPhone(me.id),
                    name: me.name ?? null,
                } : null;
                console.log('WhatsApp Bridge is now Connected and Online!');
                console.log(`Sending from: ${linkedAccount?.phone ?? 'unknown'} (${linkedAccount?.jid ?? ''})`);
                console.log(`Allowlist: ${ALLOWED_JIDS.length ? ALLOWED_JIDS.join(', ') : 'all contacts'}`);

                // Seed known contact LID mappings (Baileys 7)
                seedLidMapping(sock, KNOWN_LID_MAPPINGS).catch((e) => {
                    console.warn('Could not seed LID mappings:', e.message);
                });
            }
        });

        return sock;
    } catch (err) {
        isConnected = false;
        connecting = false;
        whatsappSocket = null;
        console.error('Failed to connect:', err.message);
        setTimeout(() => connectToWhatsApp(), RECONNECT_DELAY_MS);
    }
}

// --- API routes (Hermes/OpenClaw-style: send + receive for AI) ---

app.get('/status', (_req, res) => {
    const stableMs = stableSince ? Date.now() - stableSince : 0;
    res.json({
        connected: isConnected,
        stable: isConnected && stableMs > 3000,
        stableForMs: stableMs,
        account: linkedAccount,
        unread: messageStore.getUnreadCount(),
        allowlist: ALLOWED_JIDS,
        webhook: WEBHOOK_URL ?? null,
        lastConflictAt: lastConflictAt || null,
        lidMappings: Object.fromEntries(lidToPhoneJid),
    });
});

/** AI polls inbound messages here */
app.get('/messages', (req, res) => {
    const since = Number(req.query.since) || 0;
    const limit = Math.min(Number(req.query.limit) || 50, 200);
    const unread = req.query.unread === 'true';
    const jid = req.query.jid ? String(req.query.jid) : null;

    res.json({
        messages: messageStore.listMessages({ since, limit, unread, jid }),
        unread: messageStore.getUnreadCount(),
    });
});

/** Latest messages regardless of read state */
app.get('/messages/latest', (req, res) => {
    const limit = Math.min(Number(req.query.limit) || 20, 100);
    res.json({ messages: messageStore.getLatest(limit) });
});

/** Mark messages as read by AI */
app.post('/messages/ack', (req, res) => {
    const { ids, all } = req.body ?? {};
    const count = messageStore.markRead({ ids: ids ?? [], all: Boolean(all) });
    res.json({ acknowledged: count, unread: messageStore.getUnreadCount() });
});

/** Resolve a number to its WhatsApp JIDs (phone + lid) — useful for debugging */
app.get('/resolve', async (req, res) => {
    try {
        const input = req.query.jid ?? req.query.number ?? req.query.phone;
        if (!input) return res.status(400).json({ error: 'Provide jid or number query param' });
        if (!isConnected || !whatsappSocket) {
            return res.status(503).json({ error: 'WhatsApp not connected yet' });
        }
        const resolved = await resolveJid(whatsappSocket, toJid(input));
        res.json(resolved);
    } catch (error) {
        res.status(500).json({ error: error.message });
    }
});

/** Privacy token status for a contact — useful for debugging 463 errors */
app.get('/privacy-token', async (req, res) => {
    try {
        const input = req.query.jid ?? req.query.number ?? req.query.phone;
        if (!input) return res.status(400).json({ error: 'Provide jid or number query param' });
        if (!isConnected || !whatsappSocket) {
            return res.status(503).json({ error: 'WhatsApp not connected yet' });
        }
        const phoneJid = toJid(input);
        const { jid } = await resolveJid(whatsappSocket, phoneJid);
        const status = await getPrivacyTokenStatus(whatsappSocket, jid);
        res.json({ jid, ...status });
    } catch (error) {
        res.status(500).json({ error: error.message });
    }
});

/** Trigger on-demand history sync from phone to pull peer tctoken */
app.post('/privacy-token/sync', async (req, res) => {
    try {
        const input = req.body?.jid ?? req.body?.number ?? req.body?.phone ?? req.query?.number;
        if (!input) return res.status(400).json({ error: 'Provide jid or number' });
        if (!isConnected || !whatsappSocket) {
            return res.status(503).json({ error: 'WhatsApp not connected yet' });
        }
        const phoneJid = toJid(input);
        const { jid, phoneJid: resolvedPhoneJid, lid } = await resolveJid(whatsappSocket, phoneJid);
        const anchor = contactAnchor(resolvedPhoneJid, lid, jid);
        if (!anchor) {
            return res.status(404).json({
                error: 'No messages in journal for this contact',
                hint: 'Have the contact message Jarvis first so the bridge has a message anchor.',
            });
        }
        const status = await syncPrivacyTokenFromPhone(whatsappSocket, jid, anchor);
        res.json({ jid, anchor: { id: anchor.id, jid: anchor.jid, text: anchor.text }, ...status });
    } catch (error) {
        res.status(500).json({ error: error.message });
    }
});

/** Send a message — accepts jid, number, or phone */
app.post('/send-message', async (req, res) => {
    try {
        const recipient = req.body.jid ?? req.body.number ?? req.body.phone;
        const { message } = req.body;

        if (!recipient || !message) {
            return res.status(400).json({
                error: 'Missing recipient or message',
                hint: 'Use jid ("15551234567@s.whatsapp.net") or number ("15551234567")',
            });
        }
        if (!isConnected || !whatsappSocket) {
            return res.status(503).json({ error: 'WhatsApp not connected yet' });
        }
        const stableMs = stableSince ? Date.now() - stableSince : 0;
        if (stableMs < 3000) {
            return res.status(503).json({
                error: 'Connection not stable yet — wait a few seconds after "Connected and Online"',
                stableForMs: stableMs,
            });
        }
        if (Date.now() - lastConflictAt < 15000) {
            return res.status(503).json({
                error: 'Session conflict — log out other WhatsApp Web sessions on your phone, then restart npm start',
            });
        }

        const phoneJid = toJid(recipient);
        const { jid, phoneJid: resolvedPhoneJid, lid } = await resolveJid(whatsappSocket, phoneJid);

        const myUser = linkedAccount?.jid?.split('@')[0]?.split(':')[0];
        const theirUser = resolvedPhoneJid.split('@')[0]?.split(':')[0];
        if (myUser && theirUser && myUser === theirUser) {
            return res.status(400).json({
                error: 'Cannot send to the linked WhatsApp account itself',
                hint: 'Use NOTIFY_PHONE for a different number — linked devices cannot message themselves.',
            });
        }

        const fromLabel = linkedAccount?.phone ?? 'unknown';
        console.log(`Sending from ${fromLabel} → ${jid} (phone ${resolvedPhoneJid}${lid ? `, lid ${lid}` : ''}): "${message}"`);

        let tokenStatus = await getPrivacyTokenStatus(whatsappSocket, jid);
        logPrivacyTokenStatus('before send', tokenStatus);

        if (!tokenStatus.ready) {
            tokenStatus = await ensurePeerPrivacyToken(whatsappSocket, jid, resolvedPhoneJid, lid);
            logPrivacyTokenStatus('after ensure', tokenStatus);
        }

        // Always attempt send — Baileys attaches tctoken when present and issues ours after send
        let sent = await whatsappSocket.sendMessage(jid, { text: message });
        rememberOutbound(sent?.key?.id);

        let deliveryStatus = await waitForDelivery(whatsappSocket, sent?.key?.id);

        // On 463 / timeout: pull token from phone then retry once
        if (!deliveryStatus || deliveryStatus === 'error') {
            if (!tokenStatus.ready) {
                tokenStatus = await ensurePeerPrivacyToken(whatsappSocket, jid, resolvedPhoneJid, lid);
            }
            logPrivacyTokenStatus('before retry', tokenStatus);
            if (tokenStatus.ready) {
                sent = await whatsappSocket.sendMessage(jid, { text: message });
                rememberOutbound(sent?.key?.id);
                deliveryStatus = await waitForDelivery(whatsappSocket, sent?.key?.id);
            }
        }

        if (deliveryStatus === 'error' || deliveryStatus === 0) {
            tokenStatus = await getPrivacyTokenStatus(whatsappSocket, jid);
            return res.status(428).json({
                error: 'Message rejected (WhatsApp error 463)',
                privacyToken: {
                    ready: tokenStatus.ready,
                    storageJid: tokenStatus.storageJid,
                    tokenBytes: tokenStatus.tokenBytes,
                },
                hint: tokenStatus.ready
                    ? 'Token is present but send still failed — account may be reachout-restricted; try sending from the phone app first.'
                    : 'Peer privacy token not on this linked device. Keep Jarvis phone unlocked with WhatsApp open, POST /privacy-token/sync?number=..., then retry send.',
            });
        }
        const deliveryLabels = { 2: 'sent', 3: 'delivered', 4: 'read' };

        const record = {
            id: sent?.key?.id ?? `out-${Date.now()}`,
            jid,
            phoneJid: resolvedPhoneJid,
            lid: lid ?? null,
            from: linkedAccount?.jid ?? 'me',
            fromMe: true,
            pushName: linkedAccount?.name ?? null,
            text: message,
            type: 'conversation',
            timestamp: Date.now(),
            direction: 'outbound',
            readByAi: true,
        };
        messageStore.appendMessage(record);

        res.json({
            status: 'success',
            from: linkedAccount,
            jid,
            lid: lid ?? null,
            phoneJid: resolvedPhoneJid,
            messageId: sent?.key?.id ?? null,
            delivery: deliveryStatus ? deliveryLabels[deliveryStatus] ?? deliveryStatus : 'unknown',
            message: `Message sent from ${fromLabel} to ${lid ?? resolvedPhoneJid}`,
        });
    } catch (error) {
        console.error('Error sending message:', error.message ?? error);
        res.status(500).json({ error: error.message ?? 'Failed to send message' });
    }
});

/** AI polls unread inbound messages (mirror of POST /send-message) */
app.get('/receive', (_req, res) => {
    const messages = messageStore.listMessages({ unread: true, limit: 50 });
    if (messages.length) messageStore.markRead({ ids: messages.map((m) => m.id) });
    res.json({ messages, count: messages.length });
});

app.listen(PORT, () => {
    console.log(`Bridge API listening on http://localhost:${PORT}`);
    console.log('AI: POST /send-message  GET /receive');
});

connectToWhatsApp().catch((err) => console.error('Unexpected error:', err));

process.on('uncaughtException', (err) => {
    console.error('Uncaught error (bridge stays up):', err.message);
});
process.on('unhandledRejection', (err) => {
    console.error('Unhandled rejection (bridge stays up):', err?.message ?? err);
});
