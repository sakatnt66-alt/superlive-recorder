// worker.js - Version 15.0 - Unified Single Write
// Writes ALL data in ONE KV put() per cycle
// Saves 1000+ writes/day -> only 1 write per cycle

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === '/api/watchlist' && request.method === 'GET') {
      return handleWatchlistList(request, env);
    }
    if (url.pathname === '/api/watchlist/add' && request.method === 'POST') {
      return handleWatchlistAdd(request, env);
    }
    if (url.pathname === '/api/watchlist/remove' && request.method === 'POST') {
      return handleWatchlistRemove(request, env);
    }
    if (url.pathname.startsWith('/api/watchlist/name/') && request.method === 'POST') {
      return handleWatchlistUpdateName(request, url, env);
    }
    if (url.pathname === '/api/active-recordings' && request.method === 'GET') {
      return handleActiveRecordings(request, env);
    }
    if (url.pathname.startsWith('/api/auto-trigger/') && request.method === 'POST') {
      return handleAutoTrigger(request, url, env);
    }
    if (url.pathname === '/api/trigger-monitor' && request.method === 'POST') {
      return handleTriggerMonitorApi(request, env);
    }
    if (url.pathname.startsWith('/api/update-state/') && request.method === 'POST') {
      return handleUpdateState(request, url, env);
    }
    if (url.pathname.startsWith('/api/delete-recording/') && request.method === 'DELETE') {
      return handleDeleteRecording(request, url, env);
    }
    if (url.pathname === '/api/cleanup' && request.method === 'POST') {
      return handleCleanup(env);
    }
    if (url.pathname.startsWith('/api/check-stop/') && request.method === 'GET') {
      return handleCheckStop(request, url, env);
    }
    if (url.pathname === '/telegram/webhook' && request.method === 'POST') {
      return handleTelegramWebhook(request, env);
    }
    if (url.pathname === '/health') {
      return jsonResponse({ status: 'ok', timestamp: new Date().toISOString() });
    }

    return new Response('Not Found', { status: 404 });
  },

  async scheduled(event, env, ctx) {
    console.log('[AUTO-CRON] Triggered at ' + new Date().toISOString());
    ctx.waitUntil(handleAutoMonitorCron(env));
  }
};

// ============================================================
// UNIFIED DATA STORAGE - Single key for everything
// ============================================================
const DATA_KEY = 'superlive:data';

function isAuthorized(request, env) {
  if (!env.AUTO_API_TOKEN) return true;
  return request.headers.get('X-Auto-Token') === env.AUTO_API_TOKEN;
}

function unauthorizedResponse() {
  return new Response(JSON.stringify({ error: 'Unauthorized' }), {
    status: 401, headers: { 'Content-Type': 'application/json' }
  });
}

function escapeHtml(text) {
  if (!text) return '';
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function jsonResponse(data, status) {
  return new Response(JSON.stringify(data), {
    status: status || 200,
    headers: { 'Content-Type': 'application/json' }
  });
}

// ============================================================
// DATA LAYER - One read, one write
// ============================================================
async function getAllData(env) {
  try {
    const data = await env.SUPERLIVE_STATE.get(DATA_KEY, 'json');
    if (data && typeof data === 'object') {
      return {
        watchlist: Array.isArray(data.watchlist) ? data.watchlist : [],
        recordings: Array.isArray(data.recordings) ? data.recordings : []
      };
    }
    return { watchlist: [], recordings: [] };
  } catch (e) {
    console.error('getAllData error:', e);
    return { watchlist: [], recordings: [] };
  }
}

async function saveAllData(env, data) {
  try {
    await env.SUPERLIVE_STATE.put(DATA_KEY, JSON.stringify({
      watchlist: data.watchlist,
      recordings: data.recordings,
      last_updated: new Date().toISOString()
    }));
    return true;
  } catch (e) {
    console.error('saveAllData error:', e);
    throw new Error('KV save failed: ' + e.message);
  }
}

// ============================================================
// WATCHLIST APIs
// ============================================================
async function handleWatchlistList(request, env) {
  if (!isAuthorized(request, env)) return unauthorizedResponse();
  try {
    const data = await getAllData(env);
    return jsonResponse({
      success: true, count: data.watchlist.length,
      watchlist: data.watchlist, timestamp: new Date().toISOString()
    });
  } catch (error) {
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleWatchlistAdd(request, env) {
  if (!isAuthorized(request, env)) return unauthorizedResponse();
  try {
    const body = await request.json();
    const streamId = String(body.stream_id || '').trim();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID' }, 400);
    }
    const data = await getAllData(env);
    if (data.watchlist.some(e => String(e.stream_id) === streamId)) {
      return jsonResponse({ success: true, message: 'Already exists', stream_id: streamId });
    }
    data.watchlist.push({
      stream_id: streamId,
      added_at: new Date().toISOString(),
      display_name: null
    });
    await saveAllData(env, data);
    return jsonResponse({ success: true, stream_id: streamId, timestamp: new Date().toISOString() });
  } catch (error) {
    console.error('handleWatchlistAdd error:', error);
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleWatchlistRemove(request, env) {
  if (!isAuthorized(request, env)) return unauthorizedResponse();
  try {
    const body = await request.json();
    const streamId = String(body.stream_id || '').trim();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID' }, 400);
    }
    const data = await getAllData(env);
    const idx = data.watchlist.findIndex(e => String(e.stream_id) === streamId);
    if (idx === -1) {
      return jsonResponse({ success: false, error: 'Not found' }, 404);
    }
    data.watchlist.splice(idx, 1);
    await saveAllData(env, data);
    return jsonResponse({ success: true, stream_id: streamId });
  } catch (error) {
    console.error('handleWatchlistRemove error:', error);
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleWatchlistUpdateName(request, url, env) {
  if (!isAuthorized(request, env)) return unauthorizedResponse();
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID' }, 400);
    }
    const body = await request.json();
    const displayName = String(body.display_name || '').trim();
    const data = await getAllData(env);
    const entry = data.watchlist.find(e => String(e.stream_id) === streamId);
    if (!entry) {
      return jsonResponse({ success: false, error: 'Not found' }, 404);
    }
    entry.display_name = displayName || null;
    entry.name_updated_at = new Date().toISOString();
    await saveAllData(env, data);
    return jsonResponse({ success: true, stream_id: streamId, display_name: displayName });
  } catch (error) {
    console.error('handleWatchlistUpdateName error:', error);
    return jsonResponse({ error: error.message }, 500);
  }
}

// ============================================================
// RECORDING APIs
// ============================================================
async function handleActiveRecordings(request, env) {
  if (!isAuthorized(request, env)) return unauthorizedResponse();
  try {
    const data = await getAllData(env);
    const active = data.recordings.filter(r => r.status === 'recording');
    return jsonResponse({
      success: true, active_count: active.length,
      max_concurrent: 5, recordings: active,
      timestamp: new Date().toISOString()
    });
  } catch (error) {
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleAutoTrigger(request, url, env) {
  if (!isAuthorized(request, env)) return unauthorizedResponse();
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID' }, 400);
    }
    const body = await request.json();
    const streamUrl = body.stream_url || ('https://superlivetv.com/fr/livestream/' + streamId);
    const streamName = body.stream_name || '';
    const source = body.source || 'auto';

    const data = await getAllData(env);
    
    // Check watchlist
    if (!data.watchlist.some(e => String(e.stream_id) === streamId)) {
      return jsonResponse({ success: false, error: 'not_in_watchlist', stream_id: streamId }, 404);
    }

    // Check already recording
    const existing = data.recordings.find(r => String(r.stream_id) === streamId && r.status === 'recording');
    if (existing) {
      return jsonResponse({ success: false, error: 'already_recording', stream_id: streamId }, 409);
    }

    // Check concurrency
    const active = data.recordings.filter(r => r.status === 'recording');
    if (active.length >= 5) {
      return jsonResponse({
        success: false, error: 'concurrency_limit',
        active_count: active.length, max_concurrent: 5
      }, 429);
    }

    // Clean old recordings, add new one
    data.recordings = data.recordings.filter(r =>
      !(String(r.stream_id) === streamId && ['finished', 'failed', 'stopped'].includes(r.status))
    );
    data.recordings.push({
      stream_id: streamId, stream_url: streamUrl,
      stream_name: streamName || null, status: 'recording',
      started_at: new Date().toISOString(), source: source
    });
    await saveAllData(env, data);

    // Trigger recording
    const triggerResult = await triggerRecordWorkflow(env, streamUrl, streamId, streamName);
    if (triggerResult.success) {
      const nameLine = streamName ? ('\n👤 الاسم: <b>' + escapeHtml(streamName) + '</b>') : '';
      await sendTelegramMessage(env, env.TELEGRAM_CHAT_ID,
        '🤖 <b>Auto Recording بدأ</b>\n📺 البث: <code>' + streamId + '</code>' + nameLine + '\n🔗 الرابط: <a href="' + streamUrl + '">افتح</a>',
        { parse_mode: 'HTML' }
      );
      return jsonResponse({ success: true, started: true, stream_id: streamId, stream_name: streamName });
    } else {
      // Mark as failed
      const rec = data.recordings.find(r => String(r.stream_id) === streamId && r.status === 'recording');
      if (rec) {
        rec.status = 'failed';
        rec.error = triggerResult.error;
        await saveAllData(env, data);
      }
      return jsonResponse({ success: false, error: triggerResult.error }, 502);
    }
  } catch (error) {
    console.error('handleAutoTrigger error:', error);
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleTriggerMonitorApi(request, env) {
  if (!isAuthorized(request, env)) return unauthorizedResponse();
  try {
    const body = await request.json().catch(function() { return {}; });
    const source = body.source || 'api';
    const result = await triggerMonitorWorkflow(env, source);
    if (result.success) {
      return jsonResponse({ success: true, message: 'Monitor triggered' });
    } else {
      return jsonResponse({ success: false, error: result.error }, 502);
    }
  } catch (error) {
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleAutoMonitorCron(env) {
  try {
    console.log('[AUTO-CRON] Starting...');
    const data = await getAllData(env);
    if (!data.watchlist || data.watchlist.length === 0) {
      console.log('[AUTO-CRON] Watchlist empty');
      return;
    }
    console.log('[AUTO-CRON] Watchlist: ' + data.watchlist.length + ' users');
    const result = await triggerMonitorWorkflow(env, 'cloudflare_cron');
    if (result.success) {
      console.log('[AUTO-CRON] OK');
    } else {
      console.error('[AUTO-CRON] Failed: ' + result.error);
    }
  } catch (error) {
    console.error('[AUTO-CRON] Error:', error);
  }
}

async function handleUpdateState(request, url, env) {
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID' }, 400);
    }
    const body = await request.json();
    const data = await getAllData(env);
    const idx = data.recordings.findIndex(r => String(r.stream_id) === streamId && r.status === 'recording');
    if (idx >= 0) {
      data.recordings[idx] = Object.assign({}, data.recordings[idx], body);
    } else {
      data.recordings.push(Object.assign({ stream_id: streamId }, body));
    }
    await saveAllData(env, data);
    return jsonResponse({ success: true, updated: streamId });
  } catch (error) {
    console.error('handleUpdateState error:', error);
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleDeleteRecording(request, url, env) {
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID' }, 400);
    }
    const data = await getAllData(env);
    data.recordings = data.recordings.filter(r => String(r.stream_id) !== streamId);
    await saveAllData(env, data);
    return jsonResponse({ success: true, deleted: streamId });
  } catch (error) {
    console.error('handleDeleteRecording error:', error);
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleCleanup(env) {
  try {
    const data = await getAllData(env);
    const active = data.recordings.filter(r => r.status === 'recording');
    const deletedCount = data.recordings.length - active.length;
    data.recordings = active;
    await saveAllData(env, data);
    return jsonResponse({ success: true, deleted_count: deletedCount });
  } catch (error) {
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleCheckStop(request, url, env) {
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID', should_stop: false }, 400);
    }
    const data = await getAllData(env);
    const recording = data.recordings.find(r => String(r.stream_id) === streamId);
    let should_stop = false;
    let status = 'not_found';
    if (recording) {
      status = recording.status;
      if (['stopped', 'failed', 'finished'].includes(recording.status)) should_stop = true;
    }
    return jsonResponse({
      stream_id: streamId, status: status, should_stop: should_stop,
      timestamp: new Date().toISOString()
    });
  } catch (error) {
    return jsonResponse({ error: error.message, should_stop: false }, 500);
  }
}

// ============================================================
// TELEGRAM
// ============================================================
async function handleTelegramWebhook(request, env) {
  try {
    const update = await request.json();
    if (update.callback_query) return handleCallbackQuery(update.callback_query, env);
    if (update.message) return handleMessage(update.message, env);
    return new Response('OK');
  } catch (error) {
    console.error('Webhook error:', error);
    return new Response('OK');
  }
}

async function handleMessage(message, env) {
  const chatId = message.chat.id.toString();
  const text = (message.text || '').trim();
  if (chatId !== env.TELEGRAM_CHAT_ID) return new Response('OK');

  const parts = text.split(/\s+/);
  const command = parts[0].toLowerCase();
  const args = parts.slice(1);

  try {
    if (command === '/start' || command === '/help') { await sendMainMenu(chatId, env); return new Response('OK'); }
    if (command === '/status') { await handleStatus(chatId, env); return new Response('OK'); }
    if (command === '/cleanup') { await handleCleanupCommand(chatId, env); return new Response('OK'); }
    if (command === '/stop') { await handleStop(chatId, args[0] || null, env); return new Response('OK'); }
    if (command === '/addwatch') { await handleAddWatch(chatId, args[0] || null, env); return new Response('OK'); }
    if (command === '/removewatch') { await handleRemoveWatch(chatId, args[0] || null, env); return new Response('OK'); }
    if (command === '/watchlist') { await handleWatchlistCommand(chatId, env); return new Response('OK'); }
    if (command === '/testmonitor') { await handleTestMonitor(chatId, env); return new Response('OK'); }

    const streamUrl = extractStreamUrl(text);
    if (streamUrl) {
      const streamId = extractStreamId(streamUrl);
      if (streamId) { await handleRecord(chatId, streamUrl, env); return new Response('OK'); }
    }

    await sendTelegramMessage(env, chatId,
      '🤖 أرسل رابط البث للبدء!\n' +
      '<code>/addwatch 123456</code>\n' +
      '<code>/watchlist</code>\n' +
      '<code>/testmonitor</code>',
      { parse_mode: 'HTML' }
    );
  } catch (error) {
    console.error('handleMessage error:', error);
    try {
      await sendTelegramMessage(env, chatId, '❌ خطأ: ' + error.message, { parse_mode: 'HTML' });
    } catch (e) {}
  }
  return new Response('OK');
}

async function handleCallbackQuery(callbackQuery, env) {
  const chatId = callbackQuery.message.chat.id.toString();
  const data = callbackQuery.data;
  if (chatId !== env.TELEGRAM_CHAT_ID) {
    await answerCallbackQuery(callbackQuery.id, env, '❌ غير مصرح');
    return new Response('OK');
  }
  try {
    if (data === 'status') await handleStatus(chatId, env);
    else if (data === 'cleanup') await handleCleanupCommand(chatId, env);
    else if (data === 'stop_menu') await showStopMenu(chatId, env);
    else if (data.startsWith('stop:')) await handleStop(chatId, data.split(':')[1], env);
    else if (data === 'help') await sendHelp(chatId, env);
    else if (data === 'back') await sendMainMenu(chatId, env);
    else if (data === 'watchlist') await handleWatchlistCommand(chatId, env);
    else if (data === 'test_monitor') await handleTestMonitor(chatId, env);
    await answerCallbackQuery(callbackQuery.id, env, '✓');
  } catch (error) {
    console.error('Callback error:', error);
  }
  return new Response('OK');
}

async function handleRecord(chatId, streamUrl, env) {
  const streamId = extractStreamId(streamUrl);
  if (!streamId) { await sendTelegramMessage(env, chatId, '❌ رابط غير صالح.'); return; }

  const data = await getAllData(env);
  const existing = data.recordings.find(r => String(r.stream_id) === streamId && r.status === 'recording');
  if (existing) {
    await sendTelegramMessage(env, chatId, '⚠️ يُسجّل حالياً.', { parse_mode: 'HTML' });
    return;
  }

  const active = data.recordings.filter(r => r.status === 'recording');
  if (active.length >= 5) {
    await sendTelegramMessage(env, chatId, '❌ الحد الأقصى (5).');
    return;
  }

  let streamName = '';
  const watchEntry = data.watchlist.find(e => String(e.stream_id) === streamId);
  if (watchEntry && watchEntry.display_name) streamName = watchEntry.display_name;

  data.recordings = data.recordings.filter(r =>
    !(String(r.stream_id) === streamId && ['finished', 'failed', 'stopped'].includes(r.status))
  );
  data.recordings.push({
    stream_id: streamId, stream_url: streamUrl,
    stream_name: streamName || null, status: 'recording',
    started_at: new Date().toISOString(), source: 'manual'
  });
  await saveAllData(env, data);

  const triggerResult = await triggerRecordWorkflow(env, streamUrl, streamId, streamName);
  if (triggerResult.success) {
    const nameLine = streamName ? ('\n👤 الاسم: <b>' + escapeHtml(streamName) + '</b>') : '';
    await sendTelegramMessage(env, chatId,
      '🔴 <b>بدأ التسجيل!</b>\n📺 <code>' + streamId + '</code>' + nameLine,
      {
        parse_mode: 'HTML',
        reply_markup: { inline_keyboard: [
          [{ text: '📊 الحالة', callback_data: 'status' }],
          [{ text: '🛑 إيقاف', callback_data: 'stop:' + streamId }]
        ]}
      }
    );
  } else {
    await sendTelegramMessage(env, chatId, '❌ فشل: ' + triggerResult.error, { parse_mode: 'HTML' });
  }
}

async function handleStatus(chatId, env) {
  const data = await getAllData(env);
  const active = data.recordings.filter(r => r.status === 'recording');
  if (active.length === 0) {
    await sendTelegramMessage(env, chatId, '📭 لا تسجيلات', {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] }
    });
    return;
  }
  let msg = '📊 <b>التسجيلات:</b>\n';
  for (const r of active) {
    msg += '🔴 <code>' + r.stream_id + '</code> - ' + getElapsedTime(r.started_at) + '\n';
    if (r.stream_name) msg += '   ' + escapeHtml(r.stream_name) + '\n';
  }
  msg += '\n<b>النشطة:</b> ' + active.length + '/5';
  await sendTelegramMessage(env, chatId, msg, {
    parse_mode: 'HTML',
    reply_markup: { inline_keyboard: [
      [{ text: '🔄 تحديث', callback_data: 'status' }],
      [{ text: '🛑 إيقاف', callback_data: 'stop_menu' }],
      [{ text: '🔙 العودة', callback_data: 'back' }]
    ]}
  });
}

async function handleStop(chatId, streamId, env) {
  if (!streamId) { await sendTelegramMessage(env, chatId, '⚠️ حدد البث'); return; }
  const data = await getAllData(env);
  const r = data.recordings.find(x => String(x.stream_id) === streamId && x.status === 'recording');
  if (!r) { await sendTelegramMessage(env, chatId, '⚠️ غير موجود', { parse_mode: 'HTML' }); return; }
  r.status = 'stopped';
  r.stopped_at = new Date().toISOString();
  await saveAllData(env, data);
  await sendTelegramMessage(env, chatId, '🛑 تم: <code>' + streamId + '</code>', { parse_mode: 'HTML' });
}

async function showStopMenu(chatId, env) {
  const data = await getAllData(env);
  const active = data.recordings.filter(r => r.status === 'recording');
  if (active.length === 0) {
    await sendTelegramMessage(env, chatId, '✅ لا تسجيلات', {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] }
    });
    return;
  }
  const buttons = active.map(r => [{ text: '🛑 ' + r.stream_id, callback_data: 'stop:' + r.stream_id }]);
  buttons.push([{ text: '🔙 العودة', callback_data: 'back' }]);
  await sendTelegramMessage(env, chatId, '🛑 اختر:', {
    parse_mode: 'HTML', reply_markup: { inline_keyboard: buttons }
  });
}

async function handleCleanupCommand(chatId, env) {
  const data = await getAllData(env);
  const active = data.recordings.filter(r => r.status === 'recording');
  const deleted = data.recordings.length - active.length;
  data.recordings = active;
  await saveAllData(env, data);
  await sendTelegramMessage(env, chatId, '🧹 حذف ' + deleted + ' تسجيل', {
    parse_mode: 'HTML',
    reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] }
  });
}

async function handleAddWatch(chatId, streamId, env) {
  try {
    if (!streamId || !/^\d+$/.test(streamId)) {
      await sendTelegramMessage(env, chatId, '❌ مثال: <code>/addwatch 123456</code>', { parse_mode: 'HTML' });
      return;
    }
    const data = await getAllData(env);
    if (data.watchlist.some(e => String(e.stream_id) === streamId)) {
      await sendTelegramMessage(env, chatId, '⚠️ موجود مسبقاً', { parse_mode: 'HTML' });
      return;
    }
    data.watchlist.push({ stream_id: streamId, added_at: new Date().toISOString(), display_name: null });
    try {
      await saveAllData(env, data);
    } catch (e) {
      await sendTelegramMessage(env, chatId, '❌ فشل الحفظ: ' + e.message, { parse_mode: 'HTML' });
      return;
    }
    await sendTelegramMessage(env, chatId, '✅ تمت إضافة <code>' + streamId + '</code>', {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [
        [{ text: '📋 القائمة', callback_data: 'watchlist' }],
        [{ text: '🔙 العودة', callback_data: 'back' }]
      ]}
    });
  } catch (error) {
    console.error('handleAddWatch error:', error);
    await sendTelegramMessage(env, chatId, '❌ خطأ: ' + error.message, { parse_mode: 'HTML' });
  }
}

async function handleRemoveWatch(chatId, streamId, env) {
  try {
    if (!streamId || !/^\d+$/.test(streamId)) {
      await sendTelegramMessage(env, chatId, '❌ مثال: <code>/removewatch 123456</code>', { parse_mode: 'HTML' });
      return;
    }
    const data = await getAllData(env);
    const idx = data.watchlist.findIndex(e => String(e.stream_id) === streamId);
    if (idx === -1) {
      await sendTelegramMessage(env, chatId, '⚠️ غير موجود', { parse_mode: 'HTML' });
      return;
    }
    data.watchlist.splice(idx, 1);
    await saveAllData(env, data);
    await sendTelegramMessage(env, chatId, '🗑️ تم حذف <code>' + streamId + '</code>', {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] }
    });
  } catch (error) {
    console.error('handleRemoveWatch error:', error);
    await sendTelegramMessage(env, chatId, '❌ خطأ: ' + error.message, { parse_mode: 'HTML' });
  }
}

async function handleWatchlistCommand(chatId, env) {
  try {
    const data = await getAllData(env);
    if (data.watchlist.length === 0) {
      await sendTelegramMessage(env, chatId, '📋 فارغة. أضف: <code>/addwatch 123456</code>', {
        parse_mode: 'HTML',
        reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] }
      });
      return;
    }
    let msg = '📋 <b>(' + data.watchlist.length + '):</b>\n\n';
    for (const e of data.watchlist) {
      const name = e.display_name ? (' → ' + escapeHtml(e.display_name)) : '';
      msg += '• <code>' + e.stream_id + '</code>' + name + '\n';
    }
    await sendTelegramMessage(env, chatId, msg, {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] }
    });
  } catch (error) {
    console.error('handleWatchlistCommand error:', error);
    await sendTelegramMessage(env, chatId, '❌ خطأ: ' + error.message, { parse_mode: 'HTML' });
  }
}

async function handleTestMonitor(chatId, env) {
  await sendTelegramMessage(env, chatId, '🧪 جاري التشغيل...', { parse_mode: 'HTML' });
  const result = await triggerMonitorWorkflow(env, 'telegram_button');
  if (result.success) {
    await sendTelegramMessage(env, chatId, '✅ تم', { parse_mode: 'HTML' });
  } else {
    await sendTelegramMessage(env, chatId, '❌ فشل: ' + result.error, { parse_mode: 'HTML' });
  }
}

async function sendHelp(chatId, env) {
  await sendTelegramMessage(env, chatId,
    '🤖 <b>الأوامر:</b>\n' +
    '<code>/addwatch 123456</code>\n' +
    '<code>/watchlist</code>\n' +
    '<code>/testmonitor</code>',
    { parse_mode: 'HTML', reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] } }
  );
}

async function sendMainMenu(chatId, env) {
  const data = await getAllData(env);
  const active = data.recordings.filter(r => r.status === 'recording').length;
  await sendTelegramMessage(env, chatId,
    '🎬 <b>SuperLive Recorder</b>\n' +
    '📊 النشطة: <b>' + active + '/5</b>\n' +
    '📋 المراقبة: <b>' + data.watchlist.length + '</b>',
    {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [
        [{ text: '📊 الحالة', callback_data: 'status' }, { text: '🛑 إيقاف', callback_data: 'stop_menu' }],
        [{ text: '📋 القائمة', callback_data: 'watchlist' }, { text: '🧪 تشغيل', callback_data: 'test_monitor' }],
        [{ text: '🧹 تنظيف', callback_data: 'cleanup' }, { text: '❓ مساعدة', callback_data: 'help' }]
      ]}
    }
  );
}

function extractStreamUrl(text) {
  const urls = text.match(/https?:\/\/[^\s]+/g);
  if (urls) return urls.find(u => u.includes('superlivetv.com')) || null;
  return null;
}

function extractStreamId(url) {
  const m = url.match(/livestream\/(\d+)/);
  return m ? m[1] : null;
}

function getElapsedTime(isoString) {
  const diff = Date.now() - new Date(isoString).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return mins + 'د';
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + 'س ' + (mins % 60) + 'د';
  return Math.floor(hrs / 24) + 'ي ' + (hrs % 24) + 'س';
}

async function sendTelegramMessage(env, chatId, text, options) {
  options = options || {};
  try {
    await fetch('https://api.telegram.org/bot' + env.TELEGRAM_BOT_TOKEN + '/sendMessage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ chat_id: chatId, text: text, parse_mode: 'HTML' }, options))
    });
  } catch (e) {}
}

async function answerCallbackQuery(id, env, text) {
  try {
    await fetch('https://api.telegram.org/bot' + env.TELEGRAM_BOT_TOKEN + '/answerCallbackQuery', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ callback_query_id: id, text: text })
    });
  } catch (e) {}
}

async function triggerRecordWorkflow(env, streamUrl, streamId, streamName) {
  return triggerGitHubDispatch(env, 'record_stream', {
    stream_url: streamUrl, stream_id: streamId, stream_name: streamName || ''
  });
}

async function triggerMonitorWorkflow(env, source) {
  return triggerGitHubDispatch(env, 'auto_monitor', { source: source || 'unknown' });
}

async function triggerGitHubDispatch(env, eventType, payload) {
  if (!env.GITHUB_REPO) return { success: false, error: 'GITHUB_REPO غير مُعد' };
  if (!env.GITHUB_TOKEN) return { success: false, error: 'GITHUB_TOKEN غير مُعد' };
  if (!env.GITHUB_TOKEN.startsWith('ghp_') && !env.GITHUB_TOKEN.startsWith('github_pat_')) {
    return { success: false, error: 'GITHUB_TOKEN خاطئ' };
  }
  try {
    const response = await fetch('https://api.github.com/repos/' + env.GITHUB_REPO + '/dispatches', {
      method: 'POST',
      headers: {
        'Authorization': 'token ' + env.GITHUB_TOKEN,
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'SuperLive-Worker'
      },
      body: JSON.stringify({ event_type: eventType, client_payload: payload })
    });
    if (response.status === 204) return { success: true };
    return { success: false, error: 'HTTP ' + response.status };
  } catch (e) {
    return { success: false, error: e.message };
  }
}
