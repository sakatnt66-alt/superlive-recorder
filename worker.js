// worker.js - SuperLive Recorder + Auto Monitor + Watchlist + Cron
// Version: 10.0 - Full integration

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
        status: 'ok',
        timestamp: new Date().toISOString()
      }), {
        headers: { 'Content-Type': 'application/json' }
      });
    }

    return new Response('Not Found', { status: 404 });
  },

  async scheduled(event, env, ctx) {
    console.log('[AUTO-CRON] Cron triggered at ' + new Date().toISOString());
    ctx.waitUntil(handleAutoMonitorCron(env));
  }
};

function isAutoApiAuthorized(request, env) {
  if (!env.AUTO_API_TOKEN) return true;
  const token = request.headers.get('X-Auto-Token');
  return token === env.AUTO_API_TOKEN;
}

function unauthorizedResponse() {
  return new Response(JSON.stringify({ error: 'Unauthorized' }), {
    status: 401,
    headers: { 'Content-Type': 'application/json' }
  });
}

function escapeHtml(text) {
  if (!text) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

async function getWatchlist(env) {
  const { keys } = await env.SUPERLIVE_STATE.list({ prefix: 'watchlist:' });
  const watchlist = [];
  for (const key of keys) {
    try {
      const entry = await env.SUPERLIVE_STATE.get(key.name, 'json');
      if (entry) {
        if (!entry.stream_id) entry.stream_id = key.name.replace('watchlist:', '');
        watchlist.push(entry);
      }
    } catch (e) {}
  }
  return watchlist;
}

async function handleWatchlistList(request, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const watchlist = await getWatchlist(env);
    return new Response(JSON.stringify({
      success: true, count: watchlist.length,
      watchlist: watchlist, timestamp: new Date().toISOString()
    }), { headers: { 'Content-Type': 'application/json' } });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function handleWatchlistAdd(request, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const body = await request.json();
    const streamId = String(body.stream_id || '').trim();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return new Response(JSON.stringify({ error: 'Invalid stream ID' }), {
        status: 400, headers: { 'Content-Type': 'application/json' }
      });
    }
    const key = 'watchlist:' + streamId;
    const existing = await env.SUPERLIVE_STATE.get(key, 'json');
    if (existing) {
      return new Response(JSON.stringify({
        success: true, message: 'Already in watchlist', stream_id: streamId
      }), { headers: { 'Content-Type': 'application/json' } });
    }
    await env.SUPERLIVE_STATE.put(key, JSON.stringify({
      stream_id: streamId, added_at: new Date().toISOString(), display_name: null
    }));
    return new Response(JSON.stringify({
      success: true, stream_id: streamId, timestamp: new Date().toISOString()
    }), { headers: { 'Content-Type': 'application/json' } });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function handleWatchlistRemove(request, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const body = await request.json();
    const streamId = String(body.stream_id || '').trim();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return new Response(JSON.stringify({ error: 'Invalid stream ID' }), {
        status: 400, headers: { 'Content-Type': 'application/json' }
      });
    }
    const key = 'watchlist:' + streamId;
    const existing = await env.SUPERLIVE_STATE.get(key, 'json');
    if (!existing) {
      return new Response(JSON.stringify({
        success: false, error: 'Not found in watchlist'
      }), { status: 404, headers: { 'Content-Type': 'application/json' } });
    }
    await env.SUPERLIVE_STATE.delete(key);
    return new Response(JSON.stringify({
      success: true, stream_id: streamId, timestamp: new Date().toISOString()
    }), { headers: { 'Content-Type': 'application/json' } });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function handleWatchlistUpdateName(request, url, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return new Response(JSON.stringify({ error: 'Invalid stream ID' }), {
        status: 400, headers: { 'Content-Type': 'application/json' }
      });
    }
    const body = await request.json();
    const displayName = String(body.display_name || '').trim();
    const key = 'watchlist:' + streamId;
    const entry = await env.SUPERLIVE_STATE.get(key, 'json');
    if (!entry) {
      return new Response(JSON.stringify({
        success: false, error: 'Not found in watchlist'
      }), { status: 404, headers: { 'Content-Type': 'application/json' } });
    }
    entry.display_name = displayName || null;
    entry.name_updated_at = new Date().toISOString();
    await env.SUPERLIVE_STATE.put(key, JSON.stringify(entry));
    return new Response(JSON.stringify({
      success: true, stream_id: streamId,
      display_name: displayName, timestamp: new Date().toISOString()
    }), { headers: { 'Content-Type': 'application/json' } });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function handleActiveRecordings(request, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const { keys } = await env.SUPERLIVE_STATE.list({ prefix: 'recording:' });
    const activeRecordings = [];
    for (const key of keys) {
      try {
        const recording = await env.SUPERLIVE_STATE.get(key.name, 'json');
        if (recording && recording.status === 'recording') activeRecordings.push(recording);
      } catch (e) {}
    }
    return new Response(JSON.stringify({
      success: true, active_count: activeRecordings.length,
      max_concurrent: 5, recordings: activeRecordings,
      timestamp: new Date().toISOString()
    }), { headers: { 'Content-Type': 'application/json' } });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function handleAutoTrigger(request, url, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return new Response(JSON.stringify({ error: 'Invalid stream ID' }), {
        status: 400, headers: { 'Content-Type': 'application/json' }
      });
    }
    const body = await request.json();
    const streamUrl = body.stream_url || ('https://superlivetv.com/fr/livestream/' + streamId);
    const streamName = body.stream_name || '';
    const source = body.source || 'auto';

    const watchlistKey = 'watchlist:' + streamId;
    const watchlistEntry = await env.SUPERLIVE_STATE.get(watchlistKey, 'json');
    if (!watchlistEntry) {
      return new Response(JSON.stringify({
        success: false, error: 'not_in_watchlist', stream_id: streamId
      }), { status: 404, headers: { 'Content-Type': 'application/json' } });
    }

    const recordingKey = 'recording:' + streamId;
    const existing = await env.SUPERLIVE_STATE.get(recordingKey, 'json');
    if (existing && existing.status === 'recording') {
      return new Response(JSON.stringify({
        success: false, error: 'already_recording', stream_id: streamId
      }), { status: 409, headers: { 'Content-Type': 'application/json' } });
    }

    await autoCleanup(env);
    const activeCount = await countActiveRecordings(env);
    if (activeCount >= 5) {
      return new Response(JSON.stringify({
        success: false, error: 'concurrency_limit',
        active_count: activeCount, max_concurrent: 5
      }), { status: 429, headers: { 'Content-Type': 'application/json' } });
    }

    const recordingState = {
      stream_id: streamId, stream_url: streamUrl,
      stream_name: streamName || null, status: 'recording',
      started_at: new Date().toISOString(), duration_minutes: null,
      github_run_id: null, file_size_mb: 0, telegram_sent: false,
      error: null, source: source
    };
    await env.SUPERLIVE_STATE.put(recordingKey, JSON.stringify(recordingState));

    const triggerResult = await triggerRecordWorkflow(env, streamUrl, streamId, streamName);

    if (triggerResult.success) {
      const nameLine = streamName ? ('\n👤 الاسم: <b>' + escapeHtml(streamName) + '</b>') : '';
      await sendTelegramMessage(env, env.TELEGRAM_CHAT_ID,
        '🤖 <b>Auto Recording بدأ</b>\n📺 البث: <code>' + streamId + '</code>' + nameLine + '\n🔗 الرابط: <a href="' + streamUrl + '">افتح</a>',
        { parse_mode: 'HTML' }
      );
      return new Response(JSON.stringify({
        success: true, started: true, stream_id: streamId,
        stream_name: streamName, timestamp: new Date().toISOString()
      }), { headers: { 'Content-Type': 'application/json' } });
    } else {
      recordingState.status = 'failed';
      recordingState.error = triggerResult.error;
      await env.SUPERLIVE_STATE.put(recordingKey, JSON.stringify(recordingState));
      return new Response(JSON.stringify({
        success: false, error: triggerResult.error
      }), { status: 502, headers: { 'Content-Type': 'application/json' } });
    }
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function handleTriggerMonitorApi(request, env) {
  if (!isAutoApiAuthorized(request, env)) return unauthorizedResponse();
  try {
    const body = await request.json().catch(function() { return {}; });
    const source = body.source || 'api';
    const result = await triggerMonitorWorkflow(env, source);
    if (result.success) {
      return new Response(JSON.stringify({
        success: true, message: 'Auto Monitor triggered',
        timestamp: new Date().toISOString()
      }), { headers: { 'Content-Type': 'application/json' } });
    } else {
      return new Response(JSON.stringify({
        success: false, error: result.error
      }), { status: 502, headers: { 'Content-Type': 'application/json' } });
    }
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function handleAutoMonitorCron(env) {
  try {
    console.log('[AUTO-CRON] Starting scheduled check...');
    const watchlist = await getWatchlist(env);
    if (!watchlist || watchlist.length === 0) {
      console.log('[AUTO-CRON] Watchlist is empty, skipping');
      return;
    }
    console.log('[AUTO-CRON] Watchlist has ' + watchlist.length + ' users');
    const result = await triggerMonitorWorkflow(env, 'cloudflare_cron');
    if (result.success) {
      console.log('[AUTO-CRON] Auto monitor triggered successfully');
    } else {
      console.error('[AUTO-CRON] Failed to trigger auto monitor: ' + result.error);
    }
  } catch (error) {
    console.error('[AUTO-CRON] Error:', error);
  }
}

async function handleUpdateState(request, url, env) {
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return new Response(JSON.stringify({ error: 'Invalid stream ID' }), {
        status: 400, headers: { 'Content-Type': 'application/json' }
      });
    }
    const body = await request.json();
    const key = 'recording:' + streamId;
    let existing = {};
    try {
      const existingData = await env.SUPERLIVE_STATE.get(key, 'json');
      if (existingData) existing = existingData;
    } catch (e) {}
    const newState = Object.assign({}, existing, body);
    await env.SUPERLIVE_STATE.put(key, JSON.stringify(newState));
    return new Response(JSON.stringify({
      success: true, updated: streamId, status: newState.status
    }), { headers: { 'Content-Type': 'application/json' } });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function handleDeleteRecording(request, url, env) {
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return new Response(JSON.stringify({ error: 'Invalid stream ID' }), {
        status: 400, headers: { 'Content-Type': 'application/json' }
      });
    }
    const key = 'recording:' + streamId;
    await env.SUPERLIVE_STATE.delete(key);
    return new Response(JSON.stringify({
      success: true, deleted: streamId, timestamp: new Date().toISOString()
    }), { headers: { 'Content-Type': 'application/json' } });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function handleCleanup(env) {
  try {
    const { keys } = await env.SUPERLIVE_STATE.list({ prefix: 'recording:' });
    let deletedCount = 0;
    const deletedIds = [];
    for (const key of keys) {
      const recording = await env.SUPERLIVE_STATE.get(key.name, 'json');
      if (recording && ['finished', 'failed', 'stopped'].includes(recording.status)) {
        await env.SUPERLIVE_STATE.delete(key.name);
        deletedCount++;
        deletedIds.push(recording.stream_id);
      }
    }
    return new Response(JSON.stringify({
      success: true, deleted_count: deletedCount, deleted_ids: deletedIds,
      timestamp: new Date().toISOString()
    }), { headers: { 'Content-Type': 'application/json' } });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function handleCheckStop(request, url, env) {
  try {
    const streamId = url.pathname.split('/').pop();
    if (!streamId || !/^\d+$/.test(streamId)) {
      return new Response(JSON.stringify({
        error: 'Invalid stream ID', should_stop: false
      }), { status: 400, headers: { 'Content-Type': 'application/json' } });
    }
    const key = 'recording:' + streamId;
    const recording = await env.SUPERLIVE_STATE.get(key, 'json');
    let should_stop = false;
    let status = 'not_found';
    if (recording) {
      status = recording.status;
      if (['stopped', 'failed', 'finished'].includes(recording.status)) should_stop = true;
    }
    return new Response(JSON.stringify({
      stream_id: streamId, status: status, should_stop: should_stop,
      timestamp: new Date().toISOString()
    }), {
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-cache, no-store' }
    });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message, should_stop: false }), {
      status: 500, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function handleTelegramWebhook(request, env) {
  try {
    const update = await request.json();
    if (update.callback_query) {
      return handleCallbackQuery(update.callback_query, env);
    }
    if (update.message) {
      return handleMessage(update.message, env);
    }
    return new Response('OK');
  } catch (error) {
    console.error('Webhook error:', error);
    return new Response('OK');
  }
}

async function handleMessage(message, env) {
  const chatId = message.chat.id.toString();
  const text = (message.text || '').trim();

  if (chatId !== env.TELEGRAM_CHAT_ID) {
    return new Response('OK');
  }

  const parts = text.split(/\s+/);
  const command = parts[0].toLowerCase();
  const args = parts.slice(1);

  try {
    if (command === '/start' || command === '/help') {
      await sendMainMenu(chatId, env);
      return new Response('OK');
    }
    if (command === '/status') {
      await handleStatus(chatId, env);
      return new Response('OK');
    }
    if (command === '/cleanup') {
      await handleCleanupCommand(chatId, env);
      return new Response('OK');
    }
    if (command === '/stop') {
      const streamId = args[0] || null;
      await handleStop(chatId, streamId, env);
      return new Response('OK');
    }
    if (command === '/addwatch') {
      const streamId = args[0] || null;
      await handleAddWatch(chatId, streamId, env);
      return new Response('OK');
    }
    if (command === '/removewatch') {
      const streamId = args[0] || null;
      await handleRemoveWatch(chatId, streamId, env);
      return new Response('OK');
    }
    if (command === '/watchlist') {
      await handleWatchlistCommand(chatId, env);
      return new Response('OK');
    }
    if (command === '/testmonitor') {
      await handleTestMonitor(chatId, env);
      return new Response('OK');
    }

    const streamUrl = extractStreamUrl(text);
    if (streamUrl) {
      const streamId = extractStreamId(streamUrl);
      if (streamId) {
        await handleRecord(chatId, streamUrl, env);
        return new Response('OK');
      }
    }

    await sendTelegramMessage(env, chatId,
      '🤖 أرسل رابط البث المباشر للبدء بالتسجيل!\n' +
      'مثال:\n<code>https://superlivetv.com/fr/livestream/123456</code>\n\n' +
      'الأوامر:\n' +
      '<code>/addwatch 123456</code> - إضافة للمراقبة\n' +
      '<code>/removewatch 123456</code> - حذف من المراقبة\n' +
      '<code>/watchlist</code> - عرض القائمة\n' +
      '<code>/testmonitor</code> - تشغيل المراقبة',
      { parse_mode: 'HTML' }
    );
  } catch (error) {
    console.error('handleMessage error:', error);
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
    if (data === 'status') {
      await handleStatus(chatId, env);
      await answerCallbackQuery(callbackQuery.id, env, '✅ تم التحديث');
    } else if (data === 'cleanup') {
      await handleCleanupCommand(chatId, env);
      await answerCallbackQuery(callbackQuery.id, env, '🧹 تم التنظيف');
    } else if (data === 'stop_menu') {
      await showStopMenu(chatId, env);
      await answerCallbackQuery(callbackQuery.id, env, '✅ اختر التسجيل');
    } else if (data.startsWith('stop:')) {
      const streamId = data.split(':')[1];
      await handleStop(chatId, streamId, env);
      await answerCallbackQuery(callbackQuery.id, env, '🛑 تم الإيقاف');
    } else if (data === 'help') {
      await sendHelp(chatId, env);
      await answerCallbackQuery(callbackQuery.id, env, '✅ عرض المساعدة');
    } else if (data === 'back') {
      await sendMainMenu(chatId, env);
      await answerCallbackQuery(callbackQuery.id, env, '✅ القائمة الرئيسية');
    } else if (data === 'watchlist') {
      await handleWatchlistCommand(chatId, env);
      await answerCallbackQuery(callbackQuery.id, env, '📋 قائمة المراقبة');
    } else if (data === 'test_monitor') {
      await handleTestMonitor(chatId, env);
      await answerCallbackQuery(callbackQuery.id, env, '🧪 تم التشغيل');
    }
  } catch (error) {
    console.error('Callback error:', error);
    await answerCallbackQuery(callbackQuery.id, env, '❌ خطأ');
  }

  return new Response('OK');
}

async function handleRecord(chatId, streamUrl, env) {
  const streamId = extractStreamId(streamUrl);
  if (!streamId) {
    await sendTelegramMessage(env, chatId, '❌ رابط غير صالح.');
    return;
  }
  const existingKey = 'recording:' + streamId;
  const existing = await env.SUPERLIVE_STATE.get(existingKey, 'json');
  if (existing && existing.status === 'recording') {
    await sendTelegramMessage(env, chatId,
      '⚠️ البث <code>' + streamId + '</code> يُسجّل حالياً.\nبدأ: ' + getElapsedTime(existing.started_at),
      { parse_mode: 'HTML' }
    );
    return;
  }
  await autoCleanup(env);
  const activeCount = await countActiveRecordings(env);
  if (activeCount >= 5) {
    await sendTelegramMessage(env, chatId,
      '❌ تم الوصول للحد الأقصى (5 تسجيلات متزامنة).\nاستخدم زر "🛑 إيقاف" لإيقاف تسجيل أولاً.'
    );
    return;
  }

  let streamName = '';
  try {
    const watchEntry = await env.SUPERLIVE_STATE.get('watchlist:' + streamId, 'json');
    if (watchEntry && watchEntry.display_name) streamName = watchEntry.display_name;
  } catch (e) {}

  const recordingState = {
    stream_id: streamId, stream_url: streamUrl,
    stream_name: streamName || null, status: 'recording',
    started_at: new Date().toISOString(), duration_minutes: null,
    github_run_id: null, file_size_mb: 0, telegram_sent: false,
    error: null, source: 'manual'
  };
  await env.SUPERLIVE_STATE.put(existingKey, JSON.stringify(recordingState));

  const triggerResult = await triggerRecordWorkflow(env, streamUrl, streamId, streamName);
  if (triggerResult.success) {
    const nameLine = streamName ? ('\n👤 الاسم: <b>' + escapeHtml(streamName) + '</b>') : '';
    await sendTelegramMessage(env, chatId,
      '🔴 <b>بدأ التسجيل المستمر!</b>\n📺 البث: <code>' + streamId + '</code>' + nameLine + '\n⏱️ سيستمر حتى ينتهي البث أو تضغط "إيقاف"\n🔗 الرابط: <a href="' + streamUrl + '">افتح</a>',
      {
        parse_mode: 'HTML',
        reply_markup: {
          inline_keyboard: [
            [{ text: '📊 الحالة', callback_data: 'status' }],
            [{ text: '🛑 إيقاف هذا التسجيل', callback_data: 'stop:' + streamId }]
          ]
        }
      }
    );
  } else {
    recordingState.status = 'failed';
    recordingState.error = triggerResult.error;
    await env.SUPERLIVE_STATE.put(existingKey, JSON.stringify(recordingState));
    await sendTelegramMessage(env, chatId,
      '❌ <b>فشل بدء التسجيل!</b>\n<b>الخطأ:</b> ' + triggerResult.error,
      { parse_mode: 'HTML' }
    );
  }
}

async function handleStatus(chatId, env) {
  await autoCleanup(env);
  const { keys } = await env.SUPERLIVE_STATE.list({ prefix: 'recording:' });
  if (keys.length === 0) {
    await sendTelegramMessage(env, chatId,
      '📭 <b>لا توجد تسجيلات حالياً</b>\nأرسل رابط بث للبدء!',
      { parse_mode: 'HTML', reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] } }
    );
    return;
  }
  let message = '📊 <b>حالة التسجيلات:</b>\n';
  let hasActive = false;
  for (const key of keys) {
    const recording = await env.SUPERLIVE_STATE.get(key.name, 'json');
    if (recording) {
      const statusEmoji = getStatusEmoji(recording.status);
      const duration = recording.started_at ? getElapsedTime(recording.started_at) : 'N/A';
      if (recording.status === 'recording') hasActive = true;
      message += statusEmoji + ' <b>البث ' + recording.stream_id + '</b>\n';
      message += '   الحالة: <code>' + recording.status + '</code>\n';
      message += '   بدأ: ' + duration + '\n';
      if (recording.stream_name) message += '   الاسم: ' + escapeHtml(recording.stream_name) + '\n';
      if (recording.file_size_mb > 0) message += '   الحجم: ' + recording.file_size_mb.toFixed(2) + ' MB\n';
      if (recording.error) message += '   خطأ: ' + recording.error + '\n';
      message += '\n';
    }
  }
  const activeCount = await countActiveRecordings(env);
  message += '\n<b>التسجيلات النشطة:</b> ' + activeCount + '/5';
  const buttons = [[{ text: '🔄 تحديث', callback_data: 'status' }]];
  if (hasActive) buttons.push([{ text: '🛑 إيقاف تسجيل', callback_data: 'stop_menu' }]);
  buttons.push([{ text: '🧹 تنظيف', callback_data: 'cleanup' }]);
  buttons.push([{ text: '🔙 القائمة الرئيسية', callback_data: 'back' }]);
  await sendTelegramMessage(env, chatId, message, {
    parse_mode: 'HTML', reply_markup: { inline_keyboard: buttons }
  });
}

async function handleStop(chatId, streamId, env) {
  if (!streamId) {
    await sendTelegramMessage(env, chatId, '⚠️ يرجى تحديد البث المراد إيقافه.\nاستخدم زر "🛑 إيقاف" من القائمة.');
    return;
  }
  const key = 'recording:' + streamId;
  const recording = await env.SUPERLIVE_STATE.get(key, 'json');
  if (!recording) {
    await sendTelegramMessage(env, chatId, '⚠️ البث <code>' + streamId + '</code> غير موجود.', { parse_mode: 'HTML' });
    return;
  }
  if (recording.status !== 'recording') {
    await sendTelegramMessage(env, chatId, '⚠️ البث <code>' + streamId + '</code> لا يُسجّل حالياً.\nالحالة: <code>' + recording.status + '</code>', { parse_mode: 'HTML' });
    return;
  }
  recording.status = 'stopped';
  recording.stopped_at = new Date().toISOString();
  await env.SUPERLIVE_STATE.put(key, JSON.stringify(recording));
  const duration = recording.started_at ? getElapsedTime(recording.started_at) : 'N/A';
  await sendTelegramMessage(env, chatId,
    '🛑 <b>تم إرسال أمر الإيقاف!</b>\n📺 البث: <code>' + streamId + '</code>\n⏱️ مدة التسجيل: ' + duration,
    {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [[{ text: '📊 الحالة', callback_data: 'status' }], [{ text: '🔙 العودة', callback_data: 'back' }]] }
    }
  );
}

async function showStopMenu(chatId, env) {
  const { keys } = await env.SUPERLIVE_STATE.list({ prefix: 'recording:' });
  const activeRecordings = [];
  for (const key of keys) {
    const recording = await env.SUPERLIVE_STATE.get(key.name, 'json');
    if (recording && recording.status === 'recording') activeRecordings.push(recording);
  }
  if (activeRecordings.length === 0) {
    await sendTelegramMessage(env, chatId, '✅ لا توجد تسجيلات نشطة حالياً.', {
      parse_mode: 'HTML', reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] }
    });
    return;
  }
  const buttons = activeRecordings.map(function(rec) {
    const duration = rec.started_at ? getElapsedTime(rec.started_at) : '';
    return [{ text: '🛑 ' + rec.stream_id + ' (' + duration + ')', callback_data: 'stop:' + rec.stream_id }];
  });
  buttons.push([{ text: '🔙 العودة', callback_data: 'back' }]);
  await sendTelegramMessage(env, chatId, '🛑 <b>اختر التسجيل المراد إيقافه:</b>', {
    parse_mode: 'HTML', reply_markup: { inline_keyboard: buttons }
  });
}

async function handleCleanupCommand(chatId, env) {
  const { keys } = await env.SUPERLIVE_STATE.list({ prefix: 'recording:' });
  let deletedCount = 0;
  for (const key of keys) {
    const recording = await env.SUPERLIVE_STATE.get(key.name, 'json');
    if (recording && ['finished', 'failed', 'stopped'].includes(recording.status)) {
      await env.SUPERLIVE_STATE.delete(key.name);
      deletedCount++;
    }
  }
  await sendTelegramMessage(env, chatId,
    '🧹 <b>تم التنظيف!</b>\nتم حذف ' + deletedCount + ' تسجيل منتهي.',
    {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [[{ text: '📊 الحالة', callback_data: 'status' }], [{ text: '🔙 العودة', callback_data: 'back' }]] }
    }
  );
}

async function handleAddWatch(chatId, streamId, env) {
  if (!streamId || !/^\d+$/.test(streamId)) {
    await sendTelegramMessage(env, chatId,
      '❌ معرف غير صالح. يجب أن يكون رقماً.\nمثال: <code>/addwatch 123456</code>',
      { parse_mode: 'HTML' }
    );
    return;
  }
  const key = 'watchlist:' + streamId;
  const existing = await env.SUPERLIVE_STATE.get(key, 'json');
  if (existing) {
    await sendTelegramMessage(env, chatId,
      '⚠️ البث <code>' + streamId + '</code> موجود مسبقاً في قائمة المراقبة.',
      { parse_mode: 'HTML' }
    );
    return;
  }
  await env.SUPERLIVE_STATE.put(key, JSON.stringify({
    stream_id: streamId, added_at: new Date().toISOString(), display_name: null
  }));
  await sendTelegramMessage(env, chatId,
    '✅ تم إضافة <code>' + streamId + '</code> إلى قائمة المراقبة.\nسيتم فحصه تلقائياً كل 3 دقائق.',
    {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [[{ text: '📋 قائمة المراقبة', callback_data: 'watchlist' }], [{ text: '🔙 العودة', callback_data: 'back' }]] }
    }
  );
}

async function handleRemoveWatch(chatId, streamId, env) {
  if (!streamId || !/^\d+$/.test(streamId)) {
    await sendTelegramMessage(env, chatId,
      '❌ معرف غير صالح. يجب أن يكون رقماً.\nمثال: <code>/removewatch 123456</code>',
      { parse_mode: 'HTML' }
    );
    return;
  }
  const key = 'watchlist:' + streamId;
  const existing = await env.SUPERLIVE_STATE.get(key, 'json');
  if (!existing) {
    await sendTelegramMessage(env, chatId,
      '⚠️ البث <code>' + streamId + '</code> غير موجود في قائمة المراقبة.',
      { parse_mode: 'HTML' }
    );
    return;
  }
  await env.SUPERLIVE_STATE.delete(key);
  await sendTelegramMessage(env, chatId,
    '🗑️ تم حذف <code>' + streamId + '</code> من قائمة المراقبة.',
    {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [[{ text: '📋 قائمة المراقبة', callback_data: 'watchlist' }], [{ text: '🔙 العودة', callback_data: 'back' }]] }
    }
  );
}

async function handleWatchlistCommand(chatId, env) {
  const watchlist = await getWatchlist(env);
  if (watchlist.length === 0) {
    await sendTelegramMessage(env, chatId,
      '📋 <b>قائمة المراقبة فارغة</b>\nأضف مستخدمين باستخدام:\n<code>/addwatch 123456</code>',
      { parse_mode: 'HTML', reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] } }
    );
    return;
  }
  let message = '📋 <b>قائمة المراقبة (' + watchlist.length + ' مستخدم):</b>\n\n';
  for (const entry of watchlist) {
    const displayId = entry.stream_id || 'unknown';
    const name = entry.display_name ? (' → ' + escapeHtml(entry.display_name)) : '';
    message += '• <code>' + displayId + '</code>' + name + '\n';
  }
  message += '\nأضف: <code>/addwatch 123456</code>\nاحذف: <code>/removewatch 123456</code>';
  await sendTelegramMessage(env, chatId, message, {
    parse_mode: 'HTML',
    reply_markup: { inline_keyboard: [[{ text: '🔄 تحديث', callback_data: 'watchlist' }], [{ text: '🔙 العودة', callback_data: 'back' }]] }
  });
}

async function handleTestMonitor(chatId, env) {
  await sendTelegramMessage(env, chatId, '🧪 جاري تشغيل Auto Monitor...', { parse_mode: 'HTML' });
  const result = await triggerMonitorWorkflow(env, 'telegram_button');
  if (result.success) {
    await sendTelegramMessage(env, chatId, '✅ تم تشغيل Auto Monitor بنجاح!', { parse_mode: 'HTML' });
  } else {
    await sendTelegramMessage(env, chatId, '❌ فشل تشغيل Auto Monitor.\n<b>الخطأ:</b> ' + result.error, { parse_mode: 'HTML' });
  }
}

async function sendHelp(chatId, env) {
  const message = '🤖 <b>دليل الاستخدام</b>\n\n' +
    '<b>🎬 التسجيل:</b>\nأرسل رابط البث مباشرة\n\n' +
    '<b>📋 المراقبة:</b>\n' +
    '• <code>/addwatch 123456</code> - إضافة\n' +
    '• <code>/removewatch 123456</code> - حذف\n' +
    '• <code>/watchlist</code> - عرض القائمة\n' +
    '• <code>/testmonitor</code> - تشغيل يدوي\n\n' +
    '<b>⚠️ الحدود:</b>\n• حد أقصى 5 تسجيلات\n• فحص تلقائي كل 3 دقائق';
  await sendTelegramMessage(env, chatId, message, {
    parse_mode: 'HTML', reply_markup: { inline_keyboard: [[{ text: '🔙 القائمة الرئيسية', callback_data: 'back' }]] }
  });
}

async function sendMainMenu(chatId, env) {
  await autoCleanup(env);
  const activeCount = await countActiveRecordings(env);
  const watchlist = await getWatchlist(env);
  const message = '🎬 <b>SuperLive Recorder</b>\n\n<b>مرحباً!</b>\n📊 التسجيلات النشطة: <b>' + activeCount + '/5</b>\n📋 قائمة المراقبة: <b>' + watchlist.length + ' مستخدم</b>\n\nلبدء التسجيل، أرسل رابط البث أو استخدم الأزرار 👇';
  const buttons = [
    [{ text: '📊 الحالة', callback_data: 'status' }, { text: '🛑 إيقاف', callback_data: 'stop_menu' }],
    [{ text: '📋 قائمة المراقبة', callback_data: 'watchlist' }, { text: '🧪 تشغيل المراقبة', callback_data: 'test_monitor' }],
    [{ text: '🧹 تنظيف', callback_data: 'cleanup' }, { text: '❓ مساعدة', callback_data: 'help' }]
  ];
  await sendTelegramMessage(env, chatId, message, {
    parse_mode: 'HTML', reply_markup: { inline_keyboard: buttons }
  });
}

async function autoCleanup(env) {
  try {
    const { keys } = await env.SUPERLIVE_STATE.list({ prefix: 'recording:' });
    for (const key of keys) {
      const recording = await env.SUPERLIVE_STATE.get(key.name, 'json');
      if (recording && ['finished', 'failed'].includes(recording.status)) {
        await env.SUPERLIVE_STATE.delete(key.name);
      }
    }
  } catch (error) {
    console.error('Auto-cleanup error:', error);
  }
}

async function countActiveRecordings(env) {
  try {
    const { keys } = await env.SUPERLIVE_STATE.list({ prefix: 'recording:' });
    let count = 0;
    for (const key of keys) {
      const recording = await env.SUPERLIVE_STATE.get(key.name, 'json');
      if (recording && recording.status === 'recording') count++;
    }
    return count;
  } catch (error) {
    console.error('Error counting:', error);
    return 0;
  }
}

function extractStreamUrl(text) {
  const urlRegex = /https?:\/\/[^\s]+/g;
  const urls = text.match(urlRegex);
  if (urls && urls.length > 0) {
    const superLiveUrl = urls.find(function(url) { return url.includes('superlivetv.com'); });
    return superLiveUrl || null;
  }
  return null;
}

function extractStreamId(url) {
  const match = url.match(/livestream\/(\d+)/);
  return match ? match[1] : null;
}

function getStatusEmoji(status) {
  const emojis = {
    'recording': '🔴', 'finished': '✅', 'failed': '❌',
    'stopped': '🛑', 'uploading': '📤', 'completed': '🎉'
  };
  return emojis[status] || '❓';
}

function getElapsedTime(isoString) {
  const start = new Date(isoString);
  const now = new Date();
  const diffMs = now - start;
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 60) return diffMins + 'د';
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return diffHours + 'س ' + (diffMins % 60) + 'د';
  const diffDays = Math.floor(diffHours / 24);
  return diffDays + 'ي ' + (diffHours % 24) + 'س';
}

async function sendTelegramMessage(env, chatId, text, options) {
  options = options || {};
  const url = 'https://api.telegram.org/bot' + env.TELEGRAM_BOT_TOKEN + '/sendMessage';
  const payload = Object.assign({ chat_id: chatId, text: text, parse_mode: 'HTML' }, options);
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!response.ok) {
      console.error('Telegram API error:', await response.text());
    }
  } catch (error) {
    console.error('Failed to send message:', error);
  }
}

async function answerCallbackQuery(callbackQueryId, env, text) {
  const url = 'https://api.telegram.org/bot' + env.TELEGRAM_BOT_TOKEN + '/answerCallbackQuery';
  try {
    await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ callback_query_id: callbackQueryId, text: text })
    });
  } catch (error) {
    console.error('Failed to answer callback:', error);
  }
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
    return { success: false, error: 'GITHUB_TOKEN خاطئ (يجب أن يبدأ بـ ghp_ أو github_pat_)' };
  }
  try {
    const repo = env.GITHUB_REPO;
    const url = 'https://api.github.com/repos/' + repo + '/dispatches';
    const requestBody = { event_type: eventType, client_payload: payload };
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': 'token ' + env.GITHUB_TOKEN,
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'SuperLive-Recorder-Worker'
      },
      body: JSON.stringify(requestBody)
    });
    if (response.status === 204) return { success: true };
    const errorText = await response.text();
    let errorMessage = 'GitHub API error: ' + response.status;
    if (response.status === 401) errorMessage = 'فشل المصادقة مع GitHub';
    else if (response.status === 403) errorMessage = 'صلاحيات غير كافية';
    else if (response.status === 404) errorMessage = 'المستودع غير موجود: ' + repo;
    else if (response.status === 422) errorMessage = 'طلب غير صالح: ' + errorText;
    return { success: false, error: errorMessage };
  } catch (error) {
    return { success: false, error: 'خطأ في الشبكة: ' + error.message };
  }
}
