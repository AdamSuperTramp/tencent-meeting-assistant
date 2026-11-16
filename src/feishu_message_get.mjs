#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const messageId = process.argv[2];
if (!messageId || !/^om_[a-zA-Z0-9_-]+$/.test(messageId)) {
  process.stdout.write(JSON.stringify({ ok:false, error:'invalid message id' }));
  process.exit(2);
}
const localCfgPath = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'config', 'runtime.local.json');
let localCfg = {};
try { localCfg = JSON.parse(fs.readFileSync(localCfgPath, 'utf8')); } catch {}
const openclawConfigPath = localCfg.openclaw_config || path.join(process.env.HOME, '.openclaw/openclaw.json');
const secretsPath = localCfg.secrets_file || path.join(process.env.HOME, '.config/openclaw-pending/secrets.json');
const cfg = JSON.parse(fs.readFileSync(openclawConfigPath, 'utf8'));
const secrets = JSON.parse(fs.readFileSync(secretsPath, 'utf8'));
const appId = secrets.FEISHU_APP_ID || cfg.channels?.feishu?.appId;
const appSecret = secrets.FEISHU_APP_SECRET;
if (!appId || !appSecret) {
  process.stdout.write(JSON.stringify({ ok:false, error:'missing feishu app credentials' }));
  process.exit(2);
}
const sdkEntry = process.env.FEISHU_NODE_SDK_ENTRY || localCfg.feishu_node_sdk_entry;
if (!sdkEntry) { process.stdout.write(JSON.stringify({ ok:false, error:'missing Feishu SDK package path' })); process.exit(2); }
const require = createRequire(sdkEntry);
const lark = require('@larksuiteoapi/node-sdk');
const client = new lark.Client({ appId, appSecret, domain: lark.Domain.Feishu, loggerLevel: lark.LoggerLevel.error });
const res = await client.im.message.get({ path: { message_id: messageId } });
const item = res?.data?.items?.[0] || {};
const raw = item?.body?.content ?? item?.content ?? '';
let parsed = {};
try { parsed = typeof raw === 'string' ? JSON.parse(raw) : (raw || {}); } catch { parsed = {}; }
function extractText(node, acc = []) {
  if (node == null) return acc;
  if (typeof node === 'string') { acc.push(node); return acc; }
  if (Array.isArray(node)) { for (const x of node) extractText(x, acc); return acc; }
  if (typeof node === 'object') {
    if (typeof node.text === 'string') acc.push(node.text);
    if (typeof node.href === 'string') acc.push(node.href);
    for (const [k, v] of Object.entries(node)) {
      if (k === 'text' || k === 'href') continue;
      if (v && (typeof v === 'object' || typeof v === 'string')) extractText(v, acc);
    }
  }
  return acc;
}
let textBody = '';
if (typeof parsed.text === 'string' && parsed.text.trim()) textBody = parsed.text;
else textBody = extractText(parsed).join('\n');
if (!textBody && typeof raw === 'string' && !raw.trim().startsWith('{')) textBody = raw;
const out = {
  ok: res?.code === 0,
  api_code: res?.code,
  api_msg: res?.msg,
  msg_type: item.msg_type || null,
  chat_id: item.chat_id || null,
  file_key: parsed.file_key || parsed.fileKey || null,
  file_name: parsed.file_name || parsed.fileName || null,
  text_preview: (textBody || '').slice(0, 500),
};
process.stdout.write(JSON.stringify(out));
process.exit(out.ok ? 0 : 1);
