// worker.js - Version 12.0 (Complete Fix + Enhanced UI)
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === '/api/watchlist' && request.method === 'GET') return handleWatchlistList(request, env);
    if (url.pathname === '/api/active-recordings' && request.method === 'GET') return handleActiveRecordings(request, env);
    if (url.pathname.startsWith('/api/auto-trigger/') && request.method === 'POST') return handleAutoTrigger(request, url, env);
    if (url.pathname.startsWith('/api/update-state/') && request.method === 'POST') return handleUpdateState(request, url, env);
    if (url.pathname.startsWith('/api/delete-recording/') && request.method === 'DELETE') return handleDeleteRecording(request, url, env);
    if (url.pathname === '/api/cleanup' && request.method === 'POST') return handleCleanup(env);
    if (url.pathname.startsWith('/api/check-stop/') && request.method === 'GET') return handleCheckStop(request, url, env);
    if (url.pathname === '/api/completed-recordings' && request.method === 'GET') return handleCompletedRecordings(request, env);
    if (url.pathname === '/telegram/webhook' && request.method === 'POST') return handleTelegramWebhook(request, env);
    if (url.pathname === '/health') return jsonResponse({ status: 'ok', timestamp: new Date().toISOString() });

    return new Response('Not Found', { status: 404 });
  },

  async scheduled(event, env, ctx) {
    console.log('[AUTO-CRON] Triggered at ' + new Date().toISOString());
    ctx.waitUntil(handleAutoMonitorCron(env));
  }
};

const WATCHLIST_KEY = 'watchlist:all';
const RECORDINGS_KEY = 'recordings:active';

function isAuthorized(request, env) {
  if (!env.AUTO_API_TOKEN) return true;
  return request.headers.get('X-Auto-Token') === env.AUTO_API_TOKEN;
}

function unauthorizedResponse() { return jsonResponse({ error: 'Unauthorized' }, 401); }

function escapeHtml(text) {
  return text ? String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') : '';
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
}

async function getWatchlist(env) {
  try {
    const data = await env.SUPERLIVE_STATE.get(WATCHLIST_KEY, 'json');
    if (data && Array.isArray(data.watchlist)) return data;
    if (Array.isArray(data)) return { watchlist: data };
    return { watchlist: [] };
  } catch (e) {
    console.error('getWatchlist error:', e);
    return { watchlist: [] };
  }
}

async function saveWatchlist(env, watchlistArray) {
  try {
    await env.SUPERLIVE_STATE.put(WATCHLIST_KEY, JSON.stringify({ watchlist: watchlistArray }));
    return true;
  } catch (e) {
    console.error('saveWatchlist error:', e);
    throw new Error('KV save failed: ' + e.message);
  }
}

async function getRecordings(env) {
  try {
    const data = await env.SUPERLIVE_STATE.get(RECORDINGS_KEY, 'json');
    if (data && Array.isArray(data.recordings)) return data;
    return { recordings: [] };
  } catch (e) {
    console.error('getRecordings error:', e);
    return { recordings: [] };
  }
}

async function saveRecordings(env, recordingsArray) {
  try {
    await env.SUPERLIVE_STATE.put(RECORDINGS_KEY, JSON.stringify({ recordings: recordingsArray }));
    return true;
  } catch (e) {
    console.error('saveRecordings error:', e);
    throw new Error('KV save failed: ' + e.message);
  }
}

async function handleWatchlistList(request, env) {
  if (!isAuthorized(request, env)) return unauthorizedResponse();
  try { return jsonResponse(await getWatchlist(env)); }
  catch (error) { return jsonResponse({ error: error.message }, 500); }
}

async function handleActiveRecordings(request, env) {
  if (!isAuthorized(request, env)) return unauthorizedResponse();
  try {
    const data = await getRecordings(env);
    const recordings = Array.isArray(data.recordings) ? data.recordings : [];
    const active = recordings.filter(r => r.status === 'recording');
    return jsonResponse({ active_count: active.length, max_concurrent: 5, recordings: active });
  } catch (error) { return jsonResponse({ error: error.message }, 500); }
}

async function handleCompletedRecordings(request, env) {
  if (!isAuthorized(request, env)) return unauthorizedResponse();
  try {
    const data = await getRecordings(env);
    const recordings = Array.isArray(data.recordings) ? data.recordings : [];
    const completed = recordings.filter(r => ['completed', 'stopped', 'failed'].includes(r.status));
    return jsonResponse({ count: completed.length, recordings: completed });
  } catch (error) { return jsonResponse({ error: error.message }, 500); }
}

async function handleAutoTrigger(request, url, env) {
  if (!isAuthorized(request, env)) return unauthorizedResponse();
  try {
    const streamId = url.pathname.split('/').pop();
    const body = await request.json();
    const streamUrl = body.stream_url || ('https://superlivetv.com/fr/livestream/' + streamId);
    const streamName = body.stream_name || '';

    const wlData = await getWatchlist(env);
    if (!wlData.watchlist.some(e => String(e.stream_id) === streamId)) {
      return jsonResponse({ success: false, error: 'not_in_watchlist' }, 404);
    }

    const recData = await getRecordings(env);
    const recordings = Array.isArray(recData.recordings) ? recData.recordings : [];

    if (recordings.some(r => String(r.stream_id) === streamId && r.status === 'recording')) {
      return jsonResponse({ success: false, error: 'already_recording' }, 409);
    }

    if (recordings.filter(r => r.status === 'recording').length >= 5) {
      return jsonResponse({ success: false, error: 'concurrency_limit' }, 429);
    }

    recordings.push({
      stream_id: streamId,
      stream_url: streamUrl,
      stream_name: streamName,
      status: 'recording',
      started_at: new Date().toISOString(),
      source: 'auto'
    });
    await saveRecordings(env, recordings);

    const triggerResult = await triggerGitHubDispatch(env, 'record_stream', {
      stream_url: streamUrl,
      stream_id: streamId,
      stream_name: streamName
    });

    if (triggerResult.success) {
      const nameLine = streamName ? ('\n👤 الاسم: <b>' + escapeHtml(streamName) + '</b>') : '';
      await sendTelegramMessage(env, env.TELEGRAM_CHAT_ID,
        '🤖 <b>Auto Recording بدأ</b>\n📺 البث: <code>' + streamId + '</code>' + nameLine + '\n🔗 الرابط: <a href="' + streamUrl + '">افتح</a>',
        { parse_mode: 'HTML' }
      );
      return jsonResponse({ success: true, started: true });
    } else {
      return jsonResponse({ success: false, error: triggerResult.error }, 502);
    }
  } catch (error) { return jsonResponse({ error: error.message }, 500); }
}

async function handleUpdateState(request, url, env) {
  try {
    const streamId = url.pathname.split('/').pop();
    const body = await request.json();
    const recData = await getRecordings(env);
    const recordings = Array.isArray(recData.recordings) ? recData.recordings : [];
    const idx = recordings.findIndex(r => String(r.stream_id) === streamId && r.status === 'recording');
    if (idx >= 0) recordings[idx] = Object.assign({}, recordings[idx], body);
    else recordings.push(Object.assign({ stream_id: streamId }, body));
    await saveRecordings(env, recordings);
    return jsonResponse({ success: true });
  } catch (error) { return jsonResponse({ error: error.message }, 500); }
}

async function handleDeleteRecording(request, url, env) {
  try {
    const streamId = url.pathname.split('/').pop();
    const recData = await getRecordings(env);
    const recordings = (Array.isArray(recData.recordings) ? recData.recordings : []).filter(r => String(r.stream_id) !== streamId);
    await saveRecordings(env, recordings);
    return jsonResponse({ success: true });
  } catch (error) { return jsonResponse({ error: error.message }, 500); }
}

async function handleCleanup(env) {
  try {
    const recData = await getRecordings(env);
    const recordings = Array.isArray(recData.recordings) ? recData.recordings : [];
    const active = recordings.filter(r => r.status === 'recording');
    await saveRecordings(env, active);
    return jsonResponse({ success: true, deleted_count: recordings.length - active.length });
  } catch (error) { return jsonResponse({ error: error.message }, 500); }
}

async function handleCheckStop(request, url, env) {
  try {
    const streamId = url.pathname.split('/').pop();
    const recData = await getRecordings(env);
    const recording = (Array.isArray(recData.recordings) ? recData.recordings : []).find(r => String(r.stream_id) === streamId);
    let should_stop = false, status = 'not_found';
    if (recording) {
      status = recording.status;
      if (['stopped', 'failed', 'finished'].includes(recording.status)) should_stop = true;
    }
    return jsonResponse({ stream_id: streamId, status, should_stop });
  } catch (error) { return jsonResponse({ error: error.message, should_stop: false }, 500); }
}

// ============================================================
// TELEGRAM WEBHOOK
// ============================================================

async function handleTelegramWebhook(request, env) {
  try {
    const update = await request.json();
    if (update.callback_query) return handleCallbackQuery(update.callback_query, env);
    if (update.message) return handleMessage(update.message, env);
    return new Response('OK');
  } catch (error) { return new Response('OK'); }
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
    if (command === '/cleanup') { await handleCleanupConfirm(chatId, env); return new Response('OK'); }
    if (command === '/stop') { await handleStop(chatId, args[0] || null, env); return new Response('OK'); }
    if (command === '/addwatch') { await handleAddWatch(chatId, args[0] || null, env); return new Response('OK'); }
    if (command === '/removewatch') { await handleRemoveWatch(chatId, args[0] || null, env); return new Response('OK'); }
    if (command === '/watchlist') { await handleWatchlistCommand(chatId, env); return new Response('OK'); }
    if (command === '/testmonitor') { await handleTestMonitor(chatId, env); return new Response('OK'); }
    if (command === '/recordings') { await handleRecordingsSearch(chatId, args[0] || null, env); return new Response('OK'); }

    // ============================================================
    // MANUAL RECORDING: Detect superlivetv.com URL in message
    // ============================================================
    const streamUrl = text.match(/https?:\/\/[^\s]+/g)?.find(u => u.includes('superlivetv.com'));
    if (streamUrl) {
      const streamId = streamUrl.match(/livestream\/(\d+)/)?.[1];
      if (streamId) {
        await handleRecord(chatId, streamUrl, streamId, env);
        return new Response('OK');
      } else {
        await sendTelegramMessage(env, chatId,
          '⚠️ الرابط لا يحتوي على معرف بث صالح.\n\nمثال صحيح:\n<code>https://superlivetv.com/fr/livestream/152643970</code>',
          { parse_mode: 'HTML' }
        );
        return new Response('OK');
      }
    }

    await sendTelegramMessage(env, chatId,
      '🤖 أرسل رابط البث للبدء!\n\n<code>/addwatch 123456</code>\n<code>/watchlist</code>',
      { parse_mode: 'HTML' }
    );
  } catch (error) {
    await sendTelegramMessage(env, chatId, '❌ خطأ: ' + error.message, { parse_mode: 'HTML' });
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
    else if (data === 'cleanup') await handleCleanupConfirm(chatId, env);
    else if (data === 'cleanup_confirm') await handleCleanupExecute(chatId, env);
    else if (data === 'cleanup_cancel') await sendMainMenu(chatId, env);
    else if (data === 'stop_menu') await showStopMenu(chatId, env);
    else if (data.startsWith('stop:')) await handleStop(chatId, data.split(':')[1], env);
    else if (data === 'back') await sendMainMenu(chatId, env);
    else if (data === 'watchlist') await handleWatchlistCommand(chatId, env);
    else if (data === 'test_monitor') await handleTestMonitor(chatId, env);
    else if (data === 'recordings') await handleRecordingsSearch(chatId, null, env);

    await answerCallbackQuery(callbackQuery.id, env, '✓');
  } catch (error) { console.error('Callback error:', error); }
  return new Response('OK');
}

// ============================================================
// WATCHLIST COMMANDS
// ============================================================

async function handleAddWatch(chatId, streamId, env) {
  try {
    if (!streamId || !/^\d+$/.test(streamId)) {
      await sendTelegramMessage(env, chatId, '❌ مثال: <code>/addwatch 123456</code>', { parse_mode: 'HTML' });
      return;
    }
    const data = await getWatchlist(env);
    if (data.watchlist.some(e => String(e.stream_id) === streamId)) {
      await sendTelegramMessage(env, chatId, '⚠️ هذا المستخدم موجود مسبقاً في القائمة.', { parse_mode: 'HTML' });
      return;
    }
    data.watchlist.push({ stream_id: streamId, added_at: new Date().toISOString(), display_name: null });
    await saveWatchlist(env, data.watchlist);
    await sendTelegramMessage(env, chatId, '✅ تمت إضافة <code>' + streamId + '</code> بنجاح.', {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [[{ text: '📋 القائمة', callback_data: 'watchlist' }], [{ text: '🔙 العودة', callback_data: 'back' }]] }
    });
  } catch (error) {
    console.error('handleAddWatch error:', error);
    await sendTelegramMessage(env, chatId, '❌ فشل الحفظ في قاعدة البيانات. يرجى المحاولة لاحقاً.', { parse_mode: 'HTML' });
  }
}

async function handleRemoveWatch(chatId, streamId, env) {
  try {
    if (!streamId || !/^\d+$/.test(streamId)) {
      await sendTelegramMessage(env, chatId, '❌ مثال: <code>/removewatch 123456</code>', { parse_mode: 'HTML' });
      return;
    }
    const data = await getWatchlist(env);
    const idx = data.watchlist.findIndex(e => String(e.stream_id) === streamId);
    if (idx === -1) {
      await sendTelegramMessage(env, chatId, '⚠️ غير موجود في القائمة.', { parse_mode: 'HTML' });
      return;
    }
    data.watchlist.splice(idx, 1);
    await saveWatchlist(env, data.watchlist);
    await sendTelegramMessage(env, chatId, '🗑️ تم حذف <code>' + streamId + '</code>.', { parse_mode: 'HTML' });
  } catch (error) {
    await sendTelegramMessage(env, chatId, '❌ فشل الحذف. يرجى المحاولة لاحقاً.', { parse_mode: 'HTML' });
  }
}

async function handleWatchlistCommand(chatId, env) {
  try {
    const data = await getWatchlist(env);
    if (data.watchlist.length === 0) {
      await sendTelegramMessage(env, chatId, '📋 القائمة فارغة.', { parse_mode: 'HTML' });
      return;
    }
    let msg = '📋 <b>قائمة المراقبة (' + data.watchlist.length + '):</b>\n\n';
    for (const e of data.watchlist) {
      const name = e.display_name ? (' → ' + escapeHtml(e.display_name)) : '';
      msg += '• <code>' + e.stream_id + '</code>' + name + '\n';
    }
    await sendTelegramMessage(env, chatId, msg, { parse_mode: 'HTML' });
  } catch (error) {
    await sendTelegramMessage(env, chatId, '❌ خطأ في جلب القائمة.', { parse_mode: 'HTML' });
  }
}

// ============================================================
// MANUAL RECORDING - FIXED
// ============================================================

async function handleRecord(chatId, streamUrl, streamId, env) {
  try {
    const recData = await getRecordings(env);
    const recordings = Array.isArray(recData.recordings) ? recData.recordings : [];

    // Check if already recording this stream
    if (recordings.some(r => String(r.stream_id) === streamId && r.status === 'recording')) {
      await sendTelegramMessage(env, chatId, '⚠️ هذا البث يُسجّل حالياً.', { parse_mode: 'HTML' });
      return;
    }

    // Check concurrency limit
    if (recordings.filter(r => r.status === 'recording').length >= 5) {
      await sendTelegramMessage(env, chatId, '❌ الحد الأقصى (5 تسجيلات متزامنة) تم الوصول إليه.', { parse_mode: 'HTML' });
      return;
    }

    // Add to active recordings
    recordings.push({
      stream_id: streamId,
      stream_url: streamUrl,
      status: 'recording',
      started_at: new Date().toISOString(),
      source: 'manual'
    });
    await saveRecordings(env, recordings);

    // Trigger GitHub Actions
    const triggerResult = await triggerGitHubDispatch(env, 'record_stream', {
      stream_url: streamUrl,
      stream_id: streamId
    });

    if (triggerResult.success) {
      await sendTelegramMessage(env, chatId,
        '🔴 <b>بدأ التسجيل اليدوي!</b>\n📺 البث: <code>' + streamId + '</code>\n🔗 الرابط: <a href="' + streamUrl + '">افتح</a>\n\n⏳ جارٍ التحقق من حالة البث...',
        {
          parse_mode: 'HTML',
          reply_markup: { inline_keyboard: [[{ text: '🛑 إيقاف التسجيل', callback_data: 'stop:' + streamId }]] }
        }
      );
    } else {
      // Rollback: remove from recordings if dispatch failed
      const rollbackRecordings = recordings.filter(r => !(String(r.stream_id) === streamId && r.status === 'recording' && r.source === 'manual'));
      await saveRecordings(env, rollbackRecordings);
      await sendTelegramMessage(env, chatId, '❌ فشل بدء التسجيل: ' + triggerResult.error, { parse_mode: 'HTML' });
    }
  } catch (error) {
    await sendTelegramMessage(env, chatId, '❌ خطأ في بدء التسجيل: ' + error.message, { parse_mode: 'HTML' });
  }
}

// ============================================================
// STATUS & STOP
// ============================================================

async function handleStatus(chatId, env) {
  const recData = await getRecordings(env);
  const recordings = Array.isArray(recData.recordings) ? recData.recordings : [];
  const active = recordings.filter(r => r.status === 'recording');
  
  if (active.length === 0) {
    await sendTelegramMessage(env, chatId, '📭 لا توجد تسجيلات نشطة.', { parse_mode: 'HTML' });
    return;
  }
  
  let msg = '📊 <b>التسجيلات النشطة (' + active.length + '/5):</b>\n\n';
  
  for (let i = 0; i < active.length; i++) {
    const r = active[i];
    const source = r.source === 'auto' ? '🤖 تلقائي' : '✋ يدوي';
    const name = r.stream_name || 'غير معروف';
    const startTime = new Date(r.started_at);
    const now = new Date();
    const duration = Math.floor((now - startTime) / 1000);
    const durationStr = formatDuration(duration);
    
    msg += '🔴 <b>التسجيل ' + (i + 1) + '</b> [' + source + ']\n';
    msg += '👤 الاسم: <b>' + escapeHtml(name) + '</b>\n';
    msg += '🆔 ID: <code>' + r.stream_id + '</code>\n';
    msg += '🕒 بدأ: ' + startTime.toLocaleTimeString('ar-EG', { hour: '2-digit', minute: '2-digit' }) + '\n';
    msg += '⏱️ المدة: <code>' + durationStr + '</code>\n';
    msg += '⚙️ الحالة: جارٍ التسجيل\n\n';
  }
  
  await sendTelegramMessage(env, chatId, msg, {
    parse_mode: 'HTML',
    reply_markup: { inline_keyboard: [[{ text: '🛑 إيقاف', callback_data: 'stop_menu' }], [{ text: '🔙 العودة', callback_data: 'back' }]] }
  });
}

function formatDuration(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

async function handleStop(chatId, streamId, env) {
  if (!streamId) return;
  const recData = await getRecordings(env);
  const recordings = Array.isArray(recData.recordings) ? recData.recordings : [];
  const r = recordings.find(x => String(x.stream_id) === streamId && x.status === 'recording');
  if (!r) {
    await sendTelegramMessage(env, chatId, '⚠️ التسجيل غير موجود أو متوقف بالفعل.', { parse_mode: 'HTML' });
    return;
  }
  r.status = 'stopped';
  r.stopped_at = new Date().toISOString();
  await saveRecordings(env, recordings);
  await sendTelegramMessage(env, chatId, '🛑 تم إيقاف التسجيل: <code>' + streamId + '</code>', { parse_mode: 'HTML' });
}

async function showStopMenu(chatId, env) {
  const recData = await getRecordings(env);
  const recordings = Array.isArray(recData.recordings) ? recData.recordings : [];
  const active = recordings.filter(r => r.status === 'recording');
  if (active.length === 0) {
    await sendTelegramMessage(env, chatId, '📭 لا توجد تسجيلات نشطة لإيقافها.', { parse_mode: 'HTML' });
    return;
  }
  const buttons = active.map(r => [{ text: '🛑 ' + r.stream_id, callback_data: 'stop:' + r.stream_id }]);
  buttons.push([{ text: '🔙 العودة', callback_data: 'back' }]);
  await sendTelegramMessage(env, chatId, '🛑 اختر التسجيل المراد إيقافه:', { parse_mode: 'HTML', reply_markup: { inline_keyboard: buttons } });
}

// ============================================================
// CLEANUP WITH CONFIRMATION
// ============================================================

async function handleCleanupConfirm(chatId, env) {
  try {
    const recData = await getRecordings(env);
    const recordings = Array.isArray(recData.recordings) ? recData.recordings : [];
    const active = recordings.filter(r => r.status === 'recording');
    const inactive = recordings.length - active.length;

    if (inactive === 0) {
      await sendTelegramMessage(env, chatId, '✅ لا توجد تسجيلات منتهية لتنظيفها.', { parse_mode: 'HTML' });
      return;
    }

    await sendTelegramMessage(env, chatId,
      '🧹 <b>تنظيف قائمة التسجيلات</b>\n\n' +
      '📊 إجمالي التسجيلات: <b>' + recordings.length + '</b>\n' +
      '🔴 نشطة (لن تُحذف): <b>' + active.length + '</b>\n' +
      '🗑️ منتهية (ستُحذف): <b>' + inactive + '</b>\n\n' +
      '⚠️ سيتم حذف التسجيلات المنتهية من القائمة فقط.\n' +
      'لن يتم حذف ملفات الفيديو أو قائمة المراقبة.\n\n' +
      'هل أنت متأكد؟',
      {
        parse_mode: 'HTML',
        reply_markup: {
          inline_keyboard: [
            [{ text: '✅ نعم، احذف', callback_data: 'cleanup_confirm' }, { text: '❌ إلغاء', callback_data: 'cleanup_cancel' }]
          ]
        }
      }
    );
  } catch (error) {
    await sendTelegramMessage(env, chatId, '❌ خطأ: ' + error.message, { parse_mode: 'HTML' });
  }
}

async function handleCleanupExecute(chatId, env) {
  try {
    const recData = await getRecordings(env);
    const recordings = Array.isArray(recData.recordings) ? recData.recordings : [];
    const active = recordings.filter(r => r.status === 'recording');
    const deletedCount = recordings.length - active.length;

    await saveRecordings(env, active);

    await sendTelegramMessage(env, chatId,
      '🧹 ✅ تم تنظيف <b>' + deletedCount + '</b> تسجيل منتهٍ بنجاح.\n\n📊 المتبقي: <b>' + active.length + '</b> تسجيل نشط.',
      {
        parse_mode: 'HTML',
        reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] }
      }
    );
  } catch (error) {
    await sendTelegramMessage(env, chatId, '❌ فشل التنظيف: ' + error.message, { parse_mode: 'HTML' });
  }
}

// ============================================================
// RECORDINGS SEARCH
// ============================================================

async function handleRecordingsSearch(chatId, query, env) {
  try {
    const recData = await getRecordings(env);
    const recordings = Array.isArray(recData.recordings) ? recData.recordings : [];
    const completed = recordings.filter(r => ['completed', 'stopped', 'failed'].includes(r.status));
    
    if (completed.length === 0) {
      await sendTelegramMessage(env, chatId, '📭 لا توجد تسجيلات مكتملة.', { parse_mode: 'HTML' });
      return;
    }
    
    // If query provided, filter
    let filtered = completed;
    if (query) {
      const q = query.toLowerCase();
      filtered = completed.filter(r => 
        String(r.stream_id).includes(q) || 
        (r.stream_name && r.stream_name.toLowerCase().includes(q))
      );
    }
    
    if (filtered.length === 0) {
      await sendTelegramMessage(env, chatId, '🔍 لا توجد نتائج للبحث: <code>' + escapeHtml(query) + '</code>', { parse_mode: 'HTML' });
      return;
    }
    
    // Show last 10 recordings
    const toShow = filtered.slice(-10).reverse();
    
    let msg = '📁 <b>التسجيلات المكتملة (' + filtered.length + '):</b>\n\n';
    
    for (const r of toShow) {
      const source = r.source === 'auto' ? '🤖' : '✋';
      const name = r.stream_name || 'غير معروف';
      const status = r.status === 'completed' ? '✅' : (r.status === 'stopped' ? '🛑' : '❌');
      const startTime = new Date(r.started_at);
      const duration = r.duration ? formatDuration(Math.floor(r.duration)) : 'غير محدد';
      const size = r.file_size ? (r.file_size / (1024*1024)).toFixed(2) + ' MB' : 'غير محدد';
      
      msg += status + ' <b>' + escapeHtml(name) + '</b> [' + source + ']\n';
      msg += '🆔 <code>' + r.stream_id + '</code>\n';
      msg += '🕒 ' + startTime.toLocaleDateString('ar-EG') + ' ' + startTime.toLocaleTimeString('ar-EG', { hour: '2-digit', minute: '2-digit' }) + '\n';
      msg += '⏱️ ' + duration + ' | 📦 ' + size + '\n\n';
    }
    
    if (filtered.length > 10) {
      msg += '\n<i>عرض آخر 10 تسجيلات فقط</i>';
    }
    
    await sendTelegramMessage(env, chatId, msg, {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [[{ text: '🔙 العودة', callback_data: 'back' }]] }
    });
  } catch (error) {
    await sendTelegramMessage(env, chatId, '❌ خطأ: ' + error.message, { parse_mode: 'HTML' });
  }
}

// ============================================================
// TEST MONITOR
// ============================================================

async function handleTestMonitor(chatId, env) {
  await sendTelegramMessage(env, chatId, '🧪 جاري تشغيل المراقبة...', { parse_mode: 'HTML' });
  const result = await triggerGitHubDispatch(env, 'auto_monitor', { source: 'telegram_button' });
  await sendTelegramMessage(env, chatId, result.success ? '✅ تم تشغيل المراقبة بنجاح' : '❌ فشل التشغيل: ' + result.error, { parse_mode: 'HTML' });
}

// ============================================================
// MAIN MENU - ENHANCED
// ============================================================

async function sendMainMenu(chatId, env) {
  const wlData = await getWatchlist(env);
  const recData = await getRecordings(env);
  const recordings = Array.isArray(recData.recordings) ? recData.recordings : [];
  const active = recordings.filter(r => r.status === 'recording').length;
  const completed = recordings.filter(r => ['completed', 'stopped', 'failed'].includes(r.status)).length;

  await sendTelegramMessage(env, chatId,
    '🎬 <b>SuperLive Recorder</b>\n\n' +
    '📊 النشطة: <b>' + active + '/5</b>\n' +
    '📁 المكتملة: <b>' + completed + '</b>\n' +
    '📋 المراقبة: <b>' + wlData.watchlist.length + '</b>\n\n' +
    '💡 أرسل رابط بث مباشر لبدء التسجيل اليدوي.',
    {
      parse_mode: 'HTML',
      reply_markup: {
        inline_keyboard: [
          [{ text: '📊 الحالة', callback_data: 'status' }, { text: '🛑 إيقاف', callback_data: 'stop_menu' }],
          [{ text: '📋 القائمة', callback_data: 'watchlist' }, { text: '📁 التسجيلات', callback_data: 'recordings' }],
          [{ text: '🧪 تشغيل مراقبة', callback_data: 'test_monitor' }, { text: '🧹 تنظيف', callback_data: 'cleanup' }]
        ]
      }
    }
  );
}

// ============================================================
// TELEGRAM API HELPERS
// ============================================================

async function sendTelegramMessage(env, chatId, text, options = {}) {
  try {
    await fetch('https://api.telegram.org/bot' + env.TELEGRAM_BOT_TOKEN + '/sendMessage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ chat_id: chatId, text: text, parse_mode: 'HTML' }, options))
    });
  } catch (e) { console.error('sendTelegramMessage error:', e); }
}

async function answerCallbackQuery(id, env, text) {
  try {
    await fetch('https://api.telegram.org/bot' + env.TELEGRAM_BOT_TOKEN + '/answerCallbackQuery', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ callback_query_id: id, text })
    });
  } catch (e) {}
}

// ============================================================
// CRON & GITHUB DISPATCH
// ============================================================

async function handleAutoMonitorCron(env) {
  try {
    const wlData = await getWatchlist(env);
    if (!wlData.watchlist || wlData.watchlist.length === 0) return;
    await triggerGitHubDispatch(env, 'auto_monitor', { source: 'cloudflare_cron' });
  } catch (error) { console.error('[AUTO-CRON] Error:', error); }
}

async function triggerGitHubDispatch(env, eventType, payload) {
  if (!env.GITHUB_REPO || !env.GITHUB_TOKEN) return { success: false, error: 'Missing credentials' };
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
    return response.status === 204 ? { success: true } : { success: false, error: 'HTTP ' + response.status };
  } catch (e) { return { success: false, error: e.message }; }
}
