import { extractMessageContent, getContentType, isLidUser, isPnUser } from '@whiskeysockets/baileys';

const DEFAULT_JID_DOMAIN = '@s.whatsapp.net';

/** Maps LID JIDs → phone JIDs (populated by lidMapping + onWhatsApp). */
export const lidToPhoneJid = new Map();

export function toJid(input) {
    const raw = String(input).trim();
    if (raw.includes('@')) return raw;
    const digits = raw.replace(/\D/g, '');
    if (!digits) throw new Error('Invalid phone number or JID');
    return `${digits}${DEFAULT_JID_DOMAIN}`;
}

export function formatLid(lid) {
    if (!lid) return null;
    return lid.includes('@') ? lid : `${lid}@lid`;
}

export function isLidJid(jid) {
    return isLidUser(jid);
}

export function jidToPhone(jid) {
    if (!jid) return null;
    if (isLidJid(jid)) {
        const mapped = lidToPhoneJid.get(jid);
        if (mapped) return jidToPhone(mapped);
        return null;
    }
    const user = jid.split('@')[0]?.split(':')[0];
    return user ? `+${user}` : null;
}

function rememberLidPair(lidJid, phoneJid) {
    if (!lidJid || !phoneJid) return;
    lidToPhoneJid.set(lidJid, phoneJid);
}

/**
 * Resolve recipient for sending (Baileys 7).
 * Uses lidMapping.getLIDForPN so phone and LID share one encryption session.
 */
export async function resolveJid(sock, input) {
    const phoneJid = toJid(input);

    const results = await sock.onWhatsApp(phoneJid);
    const result = results?.[0];
    if (!result?.exists) {
        throw new Error(`Number not on WhatsApp: ${phoneJid}`);
    }

    const resolvedPhoneJid = isPnUser(result.jid) ? result.jid : phoneJid;

    let sendJid = resolvedPhoneJid;
    let lidJid = null;

    const mapping = sock.signalRepository?.lidMapping;
    if (mapping?.getLIDForPN) {
        lidJid = await mapping.getLIDForPN(resolvedPhoneJid);
        if (lidJid) {
            sendJid = lidJid;
            rememberLidPair(lidJid, resolvedPhoneJid);
        }
    }

    // Fallback: raw LID from input if caller passed one explicitly
    if (!lidJid && isLidJid(input)) {
        sendJid = input;
        lidJid = input;
    }

    const sessionJids = [...new Set([sendJid, resolvedPhoneJid].filter(Boolean))];
    await sock.assertSessions(sessionJids, true);

    return {
        jid: sendJid,
        phoneJid: resolvedPhoneJid,
        lid: lidJid,
    };
}

/** Seed a known PN↔LID pair into Baileys 7 lid-mapping store. */
export async function seedLidMapping(sock, pairs) {
    const mapping = sock.signalRepository?.lidMapping;
    if (!mapping?.storeLIDPNMappings) return;
    await mapping.storeLIDPNMappings(pairs);
    for (const { lid, pn } of pairs) {
        rememberLidPair(lid, pn);
    }
}

export function extractText(waMessage) {
    const content = extractMessageContent(waMessage.message);
    if (!content) return null;

    const type = getContentType(content);
    switch (type) {
        case 'conversation':
            return content.conversation ?? null;
        case 'extendedTextMessage':
            return content.extendedTextMessage?.text ?? null;
        case 'imageMessage':
            return content.imageMessage?.caption
                ? `[image] ${content.imageMessage.caption}`
                : '[image]';
        case 'videoMessage':
            return content.videoMessage?.caption
                ? `[video] ${content.videoMessage.caption}`
                : '[video]';
        case 'audioMessage':
            return content.audioMessage?.ptt ? '[voice note]' : '[audio]';
        case 'documentMessage':
            return content.documentMessage?.caption
                ? `[document] ${content.documentMessage.caption}`
                : `[document] ${content.documentMessage?.fileName ?? 'file'}`;
        case 'stickerMessage':
            return '[sticker]';
        case 'locationMessage':
            return '[location]';
        case 'contactMessage':
            return `[contact] ${content.contactMessage?.displayName ?? ''}`.trim();
        case 'reactionMessage':
            return content.reactionMessage?.text
                ? `[reaction] ${content.reactionMessage.text}`
                : null;
        default:
            return type ? `[${type}]` : null;
    }
}

export function normalizeWaMessage(waMessage, direction) {
    const remoteJid = waMessage.key.remoteJid;
    const remoteJidAlt = waMessage.key.remoteJidAlt;
    if (!remoteJid || remoteJid === 'status@broadcast') return null;

    const text = extractText(waMessage);
    if (!text && direction === 'inbound') return null;

    const participant = waMessage.key.participant ?? remoteJid;
    let phoneJid;

    if (remoteJid.endsWith('@g.us')) {
        phoneJid = toJid(participant.split('@')[0].split(':')[0]);
    } else if (isLidJid(remoteJid)) {
        phoneJid = lidToPhoneJid.get(remoteJid)
            ?? (remoteJidAlt && isPnUser(remoteJidAlt) ? remoteJidAlt : remoteJid);
    } else {
        phoneJid = toJid(remoteJid.split('@')[0].split(':')[0]);
    }

    if (isLidJid(remoteJid) && remoteJidAlt && isPnUser(remoteJidAlt)) {
        rememberLidPair(remoteJid, remoteJidAlt);
    }

    return {
        id: waMessage.key.id,
        jid: remoteJid,
        phoneJid,
        lid: isLidJid(remoteJid) ? remoteJid : (remoteJidAlt && isLidJid(remoteJidAlt) ? remoteJidAlt : null),
        from: participant,
        fromMe: Boolean(waMessage.key.fromMe),
        pushName: waMessage.pushName ?? null,
        text: text ?? '',
        type: getContentType(extractMessageContent(waMessage.message)) ?? 'unknown',
        timestamp: Number(waMessage.messageTimestamp) || Date.now(),
        direction,
        readByAi: direction === 'outbound',
    };
}

export function isAllowedSender(record, allowedJids) {
    if (!allowedJids.length) return true;

    const candidates = [record.jid, record.phoneJid, record.from, record.lid]
        .filter(Boolean)
        .map((j) => (j.includes('@') ? j : toJid(j)));

    if (record.lid && lidToPhoneJid.has(record.lid)) {
        candidates.push(lidToPhoneJid.get(record.lid));
    }
    if (isLidJid(record.jid) && lidToPhoneJid.has(record.jid)) {
        candidates.push(lidToPhoneJid.get(record.jid));
    }

    return candidates.some((c) =>
        allowedJids.some((allowed) => {
            const a = allowed.includes('@') ? allowed : toJid(allowed);
            return c.split('@')[0].split(':')[0] === a.split('@')[0].split(':')[0];
        })
    );
}
