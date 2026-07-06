import {
    resolveTcTokenJid,
    isTcTokenExpired,
} from '@whiskeysockets/baileys/lib/Utils/tc-token-utils.js';

/**
 * Peer privacy token (tctoken) status for a 1:1 contact.
 *
 * Baileys stores two fields under auth `tctoken`:
 * - token      — peer's token (required on outgoing messages)
 * - senderTimestamp — when we last issued our token to them (set after send)
 *
 * The peer token arrives via privacy_token notifications or history sync,
 * NOT from issuePrivacyTokens() (that only records our issuance).
 */
export async function getPrivacyTokenStatus(sock, destinationJid) {
    const mapping = sock.signalRepository?.lidMapping;
    const getLIDForPN = mapping?.getLIDForPN?.bind(mapping);
    if (!getLIDForPN) {
        return { ready: true, storageJid: destinationJid, hasToken: true, expired: false };
    }

    const storageJid = await resolveTcTokenJid(destinationJid, getLIDForPN);
    const data = await sock.authState.keys.get('tctoken', [storageJid]);
    const entry = data?.[storageJid];
    const hasToken = (entry?.token?.length ?? 0) > 0;
    const expired = hasToken && isTcTokenExpired(entry.timestamp);

    return {
        ready: hasToken && !expired,
        storageJid,
        hasToken,
        expired,
        senderTimestamp: entry?.senderTimestamp ?? null,
        tokenBytes: entry?.token?.length ?? 0,
    };
}

export function logPrivacyTokenStatus(label, status) {
    if (status.ready) {
        console.log(
            `[tctoken] ${label}: ready (${status.tokenBytes} bytes, store ${status.storageJid})`
        );
        return;
    }
    const reason = !status.hasToken
        ? 'peer token not synced yet'
        : status.expired
          ? 'peer token expired'
          : 'unknown';
    console.log(
        `[tctoken] ${label}: ${reason} (store ${status.storageJid}` +
            (status.senderTimestamp ? `, our issuance ts ${status.senderTimestamp}` : '') +
            ')'
    );
}

/** Poll until peer token appears or timeout. Used after inbound messages / before retry. */
export async function waitForPrivacyToken(sock, destinationJid, timeoutMs = 5000, intervalMs = 400) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        const status = await getPrivacyTokenStatus(sock, destinationJid);
        if (status.ready) return status;
        await new Promise((r) => setTimeout(r, intervalMs));
    }
    return getPrivacyTokenStatus(sock, destinationJid);
}

/**
 * Pull peer tctoken from the paired phone via on-demand history sync.
 * Required on reconnects — Baileys skips automatic history sync when accountSyncCounter > 0.
 */
export async function syncPrivacyTokenFromPhone(sock, destinationJid, messageAnchor) {
    const before = await getPrivacyTokenStatus(sock, destinationJid);
    if (before.ready) return before;

    if (!sock.fetchMessageHistory) {
        console.warn('[tctoken] fetchMessageHistory unavailable on socket');
        return before;
    }

    if (!messageAnchor?.id) {
        console.warn('[tctoken] no chat message to anchor on-demand history sync');
        return before;
    }

    const remoteJid = messageAnchor.jid ?? destinationJid;
    const ts = Number(messageAnchor.timestamp) || Date.now();
    const tsMs = ts < 1e12 ? ts * 1000 : ts;

    console.log(
        `[tctoken] requesting on-demand history sync from phone for ${remoteJid} ` +
            `(anchor ${messageAnchor.id}) — keep Jarvis phone unlocked with WhatsApp open`
    );

    const waitForHistory = new Promise((resolve) => {
        const timeout = setTimeout(async () => {
            sock.ev.off('messaging-history.set', onHistory);
            const final = await getPrivacyTokenStatus(sock, destinationJid);
            if (!final.ready) {
                console.warn(
                    '[tctoken] history sync timed out (45s) — is the Jarvis phone online with WhatsApp open?'
                );
            }
            resolve(final);
        }, 45000);

        async function onHistory() {
            const status = await getPrivacyTokenStatus(sock, destinationJid);
            if (status.ready) {
                clearTimeout(timeout);
                sock.ev.off('messaging-history.set', onHistory);
                resolve(status);
            }
        }

        sock.ev.on('messaging-history.set', onHistory);
    });

    try {
        await sock.fetchMessageHistory(
            50,
            {
                remoteJid,
                id: messageAnchor.id,
                fromMe: Boolean(messageAnchor.fromMe),
            },
            tsMs
        );
    } catch (err) {
        console.warn(`[tctoken] fetchMessageHistory failed: ${err.message}`);
        return getPrivacyTokenStatus(sock, destinationJid);
    }

    const status = await waitForHistory;
    logPrivacyTokenStatus('after phone history sync', status);
    return status;
}
