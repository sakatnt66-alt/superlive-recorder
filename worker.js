// worker.js - Version 14.0 - Fixed error handling

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
      return new Response(JSON.stringify({
        status: 'ok', timestamp: new Date().toISOString()
      }), { headers: { 'Content-Type': 'application/json' } });
    }

    return new Response('Not Found', { status: 404 });
  },

  async scheduled(event, env, ctx) {
    console.log('[AUTO-CRON] Triggered at ' + new Date().toISOString());
    ctx.waitUntil(handleAutoMonitorCron(env));
  }
};

// ============================================================
// CONSTANTS
// ============================================================
const WATCHLIST_KEY = 'watchlist:all';
const RECORDINGS_KEY = 'recordings:active';

function isAutoApiAuthorized(request, env) {
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
// WATCHLIST
// ============================================================
async function getWatchlist(env) {
  try {
    const data = await env.SUPERLIVE_STATE.get(WATCHLIST_KEY, 'json');
    if (Array.isArray(data)) return data;
    return [];
  } catch (e) {
    console.error('getWatchlist error:', e);
    return [];
  }
}

async function saveWatchlist(env, watchlist) {
  try {
    await env.SUPERLIVE_STATE.put(WATCHLIST_KEY, JSON.stringify(watchlist));
    return true;
  } catch (e) {
    console.error('saveWatchlist error:', e);
    throw new Error('KV save failed: ' + e.message);
  }
}

async function handleWatchlistList(request, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const watchlist = await getWatchlist(env);
    return jsonResponse({
      success: true, count: watchlist.length,
      watchlist: watchlist, timestamp: new Date().toISOString()
    });
  } catch (error) {
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleWatchlistAdd(request, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const body = await request.json();
    const streamId = String(body.stream_id || '').trim();
    
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID' }, 400);
    }
    
    const watchlist = await getWatchlist(env);
    
    // Check if already exists
    if (watchlist.some(e => String(e.stream_id) === streamId)) {
      return jsonResponse({
        success: true, message: 'Already in watchlist', stream_id: streamId
      });
    }
    
    // Add new entry
    watchlist.push({
      stream_id: streamId,
      added_at: new Date().toISOString(),
      display_name: null
    });
    
    // Save
    await saveWatchlist(env, watchlist);
    
    return jsonResponse({
      success: true, stream_id: streamId, timestamp: new Date().toISOString()
    });
  } catch (error) {
    console.error('handleWatchlistAdd error:', error);
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleWatchlistRemove(request, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const body = await request.json();
    const streamId = String(body.stream_id || '').trim();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID' }, 400);
    }
    const watchlist = await getWatchlist(env);
    const idx = watchlist.findIndex(e => String(e.stream_id) === streamId);
    if (idx === -1) {
      return jsonResponse({ success: false, error: 'Not found in watchlist' }, 404);
    }
    watchlist.splice(idx, 1);
    await saveWatchlist(env, watchlist);
    return jsonResponse({
      success: true, stream_id: streamId, timestamp: new Date().toISOString()
    });
  } catch (error) {
    console.error('handleWatchlistRemove error:', error);
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleWatchlistUpdateName(request, url, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID' }, 400);
    }
    const body = await request.json();
    const displayName = String(body.display_name || '').trim();
    const watchlist = await getWatchlist(env);
    const entry = watchlist.find(e => String(e.stream_id) === streamId);
    if (!entry) {
      return jsonResponse({ success: false, error: 'Not found in watchlist' }, 404);
    }
    entry.display_name = displayName || null;
    entry.name_updated_at = new Date().toISOString();
    await saveWatchlist(env, watchlist);
    return jsonResponse({
      success: true, stream_id: streamId,
      display_name: displayName, timestamp: new Date().toISOString()
    });
  } catch (error) {
    console.error('handleWatchlistUpdateName error:', error);
    return jsonResponse({ error: error.message }, 500);
  }
}

// ============================================================
// RECORDINGS
// ============================================================
async function getRecordings(env) {
  try {
    const data = await env.SUPERLIVE_STATE.get(RECORDINGS_KEY, 'json');
    if (Array.isArray(data)) return data;
    return [];
  } catch (e) {
    console.error('getRecordings error:', e);
    return [];
  }
}

async function saveRecordings(env, recordings) {
  try {
    await env.SUPERLIVE_STATE.put(RECORDINGS_KEY, JSON.stringify(recordings));
    return true;
  } catch (e) {
    console.error('saveRecordings error:', e);
    throw new Error('KV save failed: ' + e.message);
  }
}

async function handleActiveRecordings(request, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const recordings = await getRecordings(env);
    const active = recordings.filter(r => r.status === 'recording');
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
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID' }, 400);
    }
    const body = await request.json();
    const streamUrl = body.stream_url || ('https://superlivetv.com/fr/livestream/' + streamId);
    const streamName = body.stream_name || '';
    const source = body.source || 'auto';

    const watchlist = await getWatchlist(env);
    if (!watchlist.some(e => String(e.stream_id) === streamId)) {
      return jsonResponse({
        success: false, error: 'not_in_watchlist', stream_id: streamId
      }, 404);
    }

    const recordings = await getRecordings(env);
    const existing = recordings.find(r => String(r.stream_id) === streamId && r.status === 'recording');
    if (existing) {
      return jsonResponse({
        success: false, error: 'already_recording', stream_id: streamId
      }, 409);
    }

    const active = recordings.filter(r => r.status === 'recording');
    if (active.length >= 5) {
      return jsonResponse({
        success: false, error: 'concurrency_limit',
        active_count: active.length, max_concurrent: 5
      }, 429);
    }

    const recordingState = {
      stream_id: streamId, stream_url: streamUrl,
      stream_name: streamName || null, status: 'recording',
      started_at: new Date().toISOString(), duration_minutes: null,
      github_run_id: null, file_size_mb: 0, telegram_sent: false,
      error: null, source: source
    };

    const filtered = recordings.filter(r =>
      !(String(r.stream_id) === streamId && ['finished', 'failed', 'stopped'].includes(r.status))
    );
    filtered.push(recordingState);
    await saveRecordings(env, filtered);

    const triggerResult = await triggerRecordWorkflow(env, streamUrl, streamId, streamName);

    if (triggerResult.success) {
      const nameLine = streamName ? ('\n👤 الاسم: <b>' + escapeHtml(streamName) + '</b>') : '';
      await sendTelegramMessage(env, env.TELEGRAM_CHAT_ID,
        '🤖 <b>Auto Recording بدأ</b>\n📺 البث: <code>' + streamId + '</code>' + nameLine + '\n🔗 الرابط: <a href="' + streamUrl + '">افتح</a>',
        { parse_mode: 'HTML' }
      );
      return jsonResponse({
        success: true, started: true, stream_id: streamId,
        stream_name: streamName, timestamp: new Date().toISOString()
      });
    } else {
      recordingState.status = 'failed';
      recordingState.error = triggerResult.error;
      const idx = filtered.findIndex(r => String(r.stream_id) === streamId && r.status === 'recording');
      if (idx >= 0) filtered[idx] = recordingState;
      await saveRecordings(env, filtered);
      return jsonResponse({ success: false, error: triggerResult.error }, 502);
    }
  } catch (error) {
    console.error('handleAutoTrigger error:', error);
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleTriggerMonitorApi(request, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const body = await request.json().catch(function() { return {}; });
    const source = body.source || 'api';
    const result = await triggerMonitorWorkflow(env, source);
    if (result.success) {
      return jsonResponse({ success: true, message: 'Auto Monitor triggered', timestamp: new Date().toISOString() });
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
    const watchlist = await getWatchlist(env);
    if (!watchlist || watchlist.length === 0) {
      console.log('[AUTO-CRON] Watchlist empty');
      return;
    }
    console.log('[AUTO-CRON] Watchlist has ' + watchlist.length + ' users');
    const result = await triggerMonitorWorkflow(env, 'cloudflare_cron');
    if (result.success) {
      console.log('[AUTO-CRON] Monitor triggered successfully');
    } else {
      console.error('[AUTO-CRON] Failed: ' + result.error);
    }
  } catch (error) {
    console.error('[AUTO-CRON] Error:', error);
  }
}

// ============================================================
// LEGACY APIs
// ============================================================
async function handleUpdateState(request, url, env) {
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return jsonResponse({ error: 'Invalid stream ID' }, 400);
    }
    const body = await request.json();
    const recordings = await getRecordings(env);
    const idx = recordings.findIndex(r => String(r.stream_id) === streamId && r.status === 'recording');
    if (idx >= 0) {
      recordings[idx] = Object.assign({}, recordings[idx], body);
    } else {
      recordings.push(Object.assign({ stream_id: streamId }, body));
    }
    await saveRecordings(env, recordings);
    return jsonResponse({ success: true, updated: streamId, status: body.status });
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
    const recordings = await getRecordings(env);
    const filtered = recordings.filter(r => String(r.stream_id) !== streamId);
    await saveRecordings(env, filtered);
    return jsonResponse({ success: true, deleted: streamId, timestamp: new Date().toISOString() });
  } catch (error) {
    console.error('handleDeleteRecording error:', error);
    return jsonResponse({ error: error.message }, 500);
  }
}

async function handleCleanup(env) {
  try {
    const recordings = await getRecordings(env);
    const active = recordings.filter(r => r.status === 'recording');
    const deletedCount = recordings.length - active.length;
    await saveRecordings(env, active);
    return jsonResponse({ success: true, deleted_count: deletedCount, timestamp: new Date().toISOString() });
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
    const recordings = await getRecordings(env);
    const recording = recordings.find(r => String(r.stream_id) === streamId);
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
      '🤖 أرسل رابط البث المباشر للبدء!\n\n' +
      'الأوامر:\n' +
      '<code>/addwatch 123456</code>\n' +
      '<code>/removewatch 123456</code>\n' +
      '<code>/watchlist</code>\n' +
      '<code>/testmonitor</code>',
      { parse_mode: 'HTML' }
    );
  } catch (error) {
    console.error('handleMessage error:', error);
    try {
      await sendTelegramMessage(env, chatId, '❌ حدث خطأ: ' + error.message, { parse_mode: 'HTML' });
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

  const recordings = await getRecordings(env);
  const existing = recordings.find(r => String(r.stream_id) === streamId && r.status === 'recording');
  if (existing) {
    await sendTelegramMessage(env, chatId,
      '⚠️ البث <code>' + streamId + '</code> يُسجّل حالياً.', { parse_mode: 'HTML' });
    return;
  }

  const active = recordings.filter(r => r.status === 'recording');
  if (active.length >= 5) {
    await sendTelegramMessage(env, chatId, '❌ الحد الأقصى (5) تم الوصول إليه.');
    return;
  }

  let streamName = '';
  const watchlist = await getWatchlist(env);
  const watchEntry = watchlist.find(e => String(e.stream_id) === streamId);
  if (watchEntry && watchEntry.display_name) streamName = watchEntry.display_name;

  const state = {
    stream_id: streamId, stream_url: streamUrl,
    stream_name: streamName || null, status: 'recording',
    started_at: new Date().toISOString(), source: 'manual'
  };

  const filtered = recordings.filter(r =>
    !(String(r.stream_id) === streamId && ['finished', 'failed', 'stopped'].includes(r.status))
  );
  filtered.push(state);
  await saveRecordings(env, filtered);

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
  const recordings = await getRecordings(env);
  const active = recordings.filter(r => r.status === 'recording');
  if (active.length === 0) {
    await sendTelegramMessage(env, chatId, '📭 لا توجد تسجيلات', {
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
  const recordings = await getRecordings(env);
  const r = recordings.find(x => String(x.stream_id) === streamId && x.status === 'recording');
  if (!r) { await sendTelegramMessage(env, chatId, '⚠️ غير موجود', { parse_mode: 'HTML' }); return; }
  r.status = 'stopped';
  r.stopped_at = new Date().toISOString();
  await saveRecordings(env, recordings);
  await sendTelegramMessage(env, chatId, '🛑 تم الإيقاف: <code>' + streamId + '</code>', { parse_mode: 'HTML' });
}

async function showStopMenu(chatId, env) {
  const recordings = await getRecordings(env);
  const active = recordings.filter(r => r.status === 'recording');
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
  const recordings = await getRecordings(env);
  const active = recordings.filter(r => r.status === 'recording');
  const deleted = recordings.length - active.length;
  await saveRecordings(env, active);
  await sendTelegramMessage(env, chatId, '🧹 تم حذف ' + deleted + ' تسجيل', {
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
    
    const watchlist = await getWatchlist(env);
    
    if (watchlist.some(e => String(e.stream_id) === streamId)) {
      await sendTelegramMessage(env, chatId, '⚠️ موجود مسبقاً', { parse_mode: 'HTML' });
      return;
    }
    
    watchlist.push({ stream_id: streamId, added_at: new Date().toISOString(), display_name: null });
    
    try {
      await saveWatchlist(env, watchlist);
    } catch (e) {
      await sendTelegramMessage(env, chatId, '❌ فشل الحفظ في التخزين: ' + e.message, { parse_mode: 'HTML' });
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
    const watchlist = await getWatchlist(env);
    const idx = watchlist.findIndex(e => String(e.stream_id) === streamId);
    if (idx === -1) {
      await sendTelegramMessage(env, chatId, '⚠️ غير موجود', { parse_mode: 'HTML' });
      return;
    }
    watchlist.splice(idx, 1);
    await saveWatchlist(env, watchlist);
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
    const watchlist = await getWatchlist(env);
    if (watchlist.length === 0) {
      await sendTelegramMessage(env, chatId, '📋 فارغة. أضف: <code>/addwatch 123456</code>', {
        parse_mode: 'HTML',
        reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] }
      });
      return;
    }
    let msg = '📋 <b>(' + watchlist.length + '):</b>\n\n';
    for (const e of watchlist) {
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
    await sendTelegramMessage(env, chatId, '✅ تم التشغيل', { parse_mode: 'HTML' });
  } else {
    await sendTelegramMessage(env, chatId, '❌ فشل: ' + result.error, { parse_mode: 'HTML' });
  }
}

async function sendHelp(chatId, env) {
  await sendTelegramMessage(env, chatId,
    '🤖 <b>الأوامر:</b>\n\n' +
    '<code>/addwatch 123456</code>\n' +
    '<code>/removewatch 123456</code>\n' +
    '<code>/watchlist</code>\n' +
    '<code>/testmonitor</code>',
    { parse_mode: 'HTML', reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] } }
  );
}

async function sendMainMenu(chatId, env) {
  const watchlist = await getWatchlist(env);
  const recordings = await getRecordings(env);
  const active = recordings.filter(r => r.status === 'recording').length;
  await sendTelegramMessage(env, chatId,
    '🎬 <b>SuperLive Recorder</b>\n\n' +
    '📊 النشطة: <b>' + active + '/5</b>\n' +
    '📋 المراقبة: <b>' + watchlist.length + '</b>',
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
